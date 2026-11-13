"""Versioned price candidates. Matching never confirms a price or calculates a quote."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid


SCHEMA_VERSION = 1
KINDS = {"material", "operation", "setup"}
UNITS = {
    "material": {"CNY/kg"},
    "operation": {"CNY/piece", "CNY/order"},
    "setup": {"CNY/order"},
}
FIELDS = {
    "kind", "name", "rate", "unit", "material_grade", "material_form",
    "thickness_min", "thickness_max", "product_family", "operation",
    "qty_min", "qty_max", "valid_from", "valid_to", "currency",
    "tax_included", "freight_included", "source", "simulated", "inclusions",
}
REQUIRED = FIELDS - {"product_family", "operation", "qty_min", "qty_max"}
CONTEXT_FIELDS = {
    "material_grade", "material_form", "thickness", "quantity",
    "product_family", "process_route", "as_of",
}
DECIMAL_TEXT = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class PriceCatalogError(ValueError):
    def __init__(self, message, code="invalid_argument"):
        super().__init__(message)
        self.code = code


def require(condition, message, code="invalid_argument"):
    if not condition:
        raise PriceCatalogError(message, code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def checksum(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, field, maximum=200):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum,
            f"{field} must be nonempty text (at most {maximum} characters)")
    require(not any(ord(char) < 32 and char not in "\n\t" for char in value), f"invalid characters in {field}")
    return value.strip()


def decimal(value, field, *, positive=False):
    require(isinstance(value, str) and len(value) <= 64 and DECIMAL_TEXT.fullmatch(value.strip()) is not None,
            f"{field} must be a nonnegative decimal string")
    number = Decimal(value.strip())
    require(number < Decimal("1000000000000000"), f"{field} is outside the supported range")
    require(not positive or number > 0, f"{field} must be greater than zero")
    # Formatting directly avoids Decimal.normalize() rounding through the ambient context.
    result = format(number, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def positive_integer(value, field):
    require(type(value) is int and 0 < value < 10**15, f"{field} must be a positive integer")
    return value


def iso_date(value, field):
    require(isinstance(value, str) and DATE_TEXT.fullmatch(value) is not None, f"{field} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise PriceCatalogError(f"{field} is not a valid date") from error


def validate_entry_id(value):
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None, "invalid entry_id")
    return value


def normalize_payload(payload):
    require(isinstance(payload, dict), "price payload must be an object")
    require(not set(payload).difference(FIELDS), "unsupported price payload fields")
    missing = sorted(REQUIRED.difference(payload))
    require(not missing, "missing price fields: " + ", ".join(missing))
    kind = payload["kind"]
    require(isinstance(kind, str) and kind in KINDS, "kind must be material, operation or setup")
    require(isinstance(payload["unit"], str) and payload["unit"] in UNITS[kind], f"unsupported unit for {kind}")
    require(payload["currency"] == "CNY", "only CNY prices are supported")
    require(payload["tax_included"] is False and payload["freight_included"] is False,
            "prices must explicitly exclude tax and freight")
    require(type(payload["simulated"]) is bool, "simulated must be an explicit boolean")
    start = iso_date(payload["valid_from"], "valid_from")
    end = iso_date(payload["valid_to"], "valid_to")
    require(start <= end, "valid_from must not follow valid_to")
    low = decimal(payload["thickness_min"], "thickness_min", positive=True)
    high = decimal(payload["thickness_max"], "thickness_max", positive=True)
    require(Decimal(low) <= Decimal(high), "thickness_min must not exceed thickness_max")
    qty_low = positive_integer(payload.get("qty_min", 1), "qty_min")
    qty_high = payload.get("qty_max")
    if qty_high is not None:
        positive_integer(qty_high, "qty_max")
        require(qty_low <= qty_high, "qty_min must not exceed qty_max")
    inclusions = payload["inclusions"]
    require(isinstance(inclusions, list) and 0 < len(inclusions) <= 40, "inclusions must be a nonempty text list")
    inclusions = [text(item, "inclusions item", 500) for item in inclusions]
    family = payload.get("product_family")
    operation = payload.get("operation")
    if kind in {"operation", "setup"}:
        family = text(family, "product_family")
    else:
        require(family is None or family == "", "material prices do not support a product_family condition")
        family = None
    if kind == "operation":
        operation = text(operation, "operation")
    else:
        require(operation is None or operation == "", "operation applies only to operation prices")
        operation = None
    return {
        "kind": kind, "name": text(payload["name"], "name"),
        "rate": decimal(payload["rate"], "rate"), "unit": payload["unit"],
        "material_grade": text(payload["material_grade"], "material_grade"),
        "material_form": text(payload["material_form"], "material_form"),
        "thickness_min": low, "thickness_max": high,
        "product_family": family, "operation": operation,
        "qty_min": qty_low, "qty_max": qty_high,
        "valid_from": start.isoformat(), "valid_to": end.isoformat(),
        "currency": "CNY", "tax_included": False, "freight_included": False,
        "source": text(payload["source"], "source", 4000),
        "simulated": payload["simulated"], "inclusions": inclusions,
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS price_entries (
    entry_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('material','operation','setup')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_versions (
    entry_id TEXT NOT NULL REFERENCES price_entries(entry_id),
    version INTEGER NOT NULL CHECK(version > 0),
    status TEXT NOT NULL CHECK(status IN ('draft','published','disabled')),
    snapshot_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    disabled_at TEXT,
    PRIMARY KEY(entry_id, version),
    CHECK(status != 'published' OR published_at IS NOT NULL),
    CHECK((status = 'disabled') = (disabled_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_published_price_version
    ON price_versions(entry_id) WHERE status = 'published';
CREATE TABLE IF NOT EXISTS price_requests (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS immutable_price_content
BEFORE UPDATE OF entry_id, version, snapshot_json, snapshot_sha256, created_at ON price_versions
BEGIN SELECT RAISE(ABORT, 'price version content is immutable'); END;
CREATE TRIGGER IF NOT EXISTS retain_price_history
BEFORE DELETE ON price_versions
BEGIN SELECT RAISE(ABORT, 'price version history cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS valid_price_lifecycle
BEFORE UPDATE OF status ON price_versions
WHEN NOT ((OLD.status = 'draft' AND NEW.status IN ('published','disabled'))
       OR (OLD.status = 'published' AND NEW.status = 'disabled'))
BEGIN SELECT RAISE(ABORT, 'invalid price lifecycle transition'); END;
CREATE TRIGGER IF NOT EXISTS immutable_publication_time
BEFORE UPDATE OF published_at ON price_versions
WHEN OLD.published_at IS NOT NULL AND NEW.published_at IS NOT OLD.published_at
BEGIN SELECT RAISE(ABORT, 'publication time is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_disable_time
BEFORE UPDATE OF disabled_at ON price_versions
WHEN OLD.disabled_at IS NOT NULL AND NEW.disabled_at IS NOT OLD.disabled_at
BEGIN SELECT RAISE(ABORT, 'disable time is immutable'); END;
"""


class PriceCatalog:
    """Dedicated SQLite catalogue; no seed rows and no automatic quote mutation."""

    def __init__(self, path):
        require(str(path) != ":memory:", "provide a persistent SQLite file path")
        self.path = Path(path).expanduser().resolve()
        require(not self.path.is_dir(), "catalogue path must be a file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            self._enable_wal(connection)
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            require(version in {0, SCHEMA_VERSION}, "unsupported catalogue schema version", "unsupported_schema")
            if version == 0:
                tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
                require(not tables, "existing database is not an initialized price catalogue", "unsupported_schema")
            # executescript() implicitly commits a pending transaction. Execute complete
            # fixed statements individually so first-use checks and schema creation share a lock.
            statement = ""
            for line in SCHEMA.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            require(not statement.strip(), "incomplete internal catalogue schema", "unsupported_schema")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    @staticmethod
    def _enable_wal(connection):
        # A first-use journal-mode upgrade can return SQLITE_BUSY immediately,
        # even with busy_timeout set. Retry only this pre-transaction operation;
        # never replay a partially applied price transaction.
        deadline = time.monotonic() + 2.0
        delay = 0.01
        connection.execute("PRAGMA busy_timeout = 250")
        try:
            while True:
                try:
                    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    require(mode.lower() == "wal", "unable to enable catalogue WAL mode", "database_busy")
                    return
                except sqlite3.OperationalError as error:
                    code = getattr(error, "sqlite_errorcode", 0) & 255
                    remaining = deadline - time.monotonic()
                    if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or remaining <= 0:
                        raise
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, 0.1)
        finally:
            connection.execute("PRAGMA busy_timeout = 10000")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            yield connection
        except sqlite3.IntegrityError as error:
            raise PriceCatalogError(str(error), "conflict") from error
        finally:
            connection.close()

    @contextmanager
    def _write(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _decode(row):
        require(row is not None, "price version not found", "not_found")
        require(checksum(row["snapshot_json"]) == row["snapshot_sha256"], "price snapshot checksum mismatch", "integrity_error")
        result = json.loads(row["snapshot_json"])
        result.update(status=row["status"], published_at=row["published_at"],
                      disabled_at=row["disabled_at"], snapshot_sha256=row["snapshot_sha256"])
        return result

    @staticmethod
    def _row(connection, identity, version):
        return connection.execute("SELECT * FROM price_versions WHERE entry_id = ? AND version = ?", (identity, version)).fetchone()

    @staticmethod
    def _request(request_id, action, payload, connection):
        signature = checksum(canonical({"action": action, "payload": payload}))
        if request_id is None:
            return signature, None
        require(isinstance(request_id, str) and 0 < len(request_id) <= 200 and request_id == request_id.strip()
                and not any(ord(char) < 32 for char in request_id), "invalid request_id")
        previous = connection.execute("SELECT action, payload_sha256, result_json FROM price_requests WHERE request_id = ?", (request_id,)).fetchone()
        if previous is None:
            return signature, None
        require(previous["action"] == action and previous["payload_sha256"] == signature,
                "request_id already belongs to different parameters or action", "conflict")
        return signature, json.loads(previous["result_json"])

    @staticmethod
    def _remember(connection, request_id, action, signature, result):
        if request_id is not None:
            connection.execute("INSERT INTO price_requests(request_id, action, payload_sha256, result_json, created_at) VALUES (?,?,?,?,?)",
                               (request_id, action, signature, canonical(result), now()))

    @staticmethod
    def _insert_version(connection, identity, version, payload, created):
        snapshot = canonical({**payload, "entry_id": identity, "version": version, "created_at": created})
        connection.execute("INSERT INTO price_versions(entry_id,version,status,snapshot_json,snapshot_sha256,created_at) VALUES (?,?,'draft',?,?,?)",
                           (identity, version, snapshot, checksum(snapshot), created))

    def list_entries(self):
        """Return all versions, including drafts and disabled historical versions."""
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM price_versions ORDER BY entry_id, version DESC").fetchall()
            return [self._decode(row) for row in rows]

    def get(self, entry_id, version):
        identity = validate_entry_id(entry_id)
        version = positive_integer(version, "version")
        with self._connection() as connection:
            return self._decode(self._row(connection, identity, version))

    def create(self, payload, *, request_id=None):
        payload = normalize_payload(payload)
        with self._write() as connection:
            signature, replay = self._request(request_id, "create", payload, connection)
            if replay is not None:
                return replay
            identity, created = "price_" + uuid.uuid4().hex, now()
            connection.execute("INSERT INTO price_entries(entry_id,kind,created_at) VALUES (?,?,?)", (identity, payload["kind"], created))
            self._insert_version(connection, identity, 1, payload, created)
            result = self._decode(self._row(connection, identity, 1))
            self._remember(connection, request_id, "create", signature, result)
            return result

    def new_version(self, entry_id, payload, *, request_id=None):
        identity, payload = validate_entry_id(entry_id), normalize_payload(payload)
        with self._write() as connection:
            signature, replay = self._request(request_id, "new_version", {"entry_id": identity, "price": payload}, connection)
            if replay is not None:
                return replay
            entry = connection.execute("SELECT kind FROM price_entries WHERE entry_id = ?", (identity,)).fetchone()
            require(entry is not None, "price entry not found", "not_found")
            require(entry["kind"] == payload["kind"], "new version cannot change price kind")
            version = connection.execute("SELECT MAX(version) FROM price_versions WHERE entry_id = ?", (identity,)).fetchone()[0] + 1
            self._insert_version(connection, identity, version, payload, now())
            result = self._decode(self._row(connection, identity, version))
            self._remember(connection, request_id, "new_version", signature, result)
            return result

    def publish(self, entry_id, version, *, request_id=None):
        identity, version = validate_entry_id(entry_id), positive_integer(version, "version")
        with self._write() as connection:
            signature, replay = self._request(request_id, "publish", {"entry_id": identity, "version": version}, connection)
            if replay is not None:
                return replay
            row = self._row(connection, identity, version)
            result = self._decode(row)
            if row["status"] != "published":
                require(row["status"] == "draft", "disabled versions cannot be republished; create a new version", "conflict")
                latest = connection.execute("SELECT MAX(version) FROM price_versions WHERE entry_id = ? AND published_at IS NOT NULL", (identity,)).fetchone()[0]
                require(latest is None or version > latest, "cannot publish a version older than publication history", "conflict")
                published = now()
                connection.execute("UPDATE price_versions SET status='disabled',disabled_at=? WHERE entry_id=? AND status='published'", (published, identity))
                connection.execute("UPDATE price_versions SET status='published',published_at=? WHERE entry_id=? AND version=?", (published, identity, version))
                result = self._decode(self._row(connection, identity, version))
            self._remember(connection, request_id, "publish", signature, result)
            return result

    def disable(self, entry_id, version, *, request_id=None):
        identity, version = validate_entry_id(entry_id), positive_integer(version, "version")
        with self._write() as connection:
            signature, replay = self._request(request_id, "disable", {"entry_id": identity, "version": version}, connection)
            if replay is not None:
                return replay
            row = self._row(connection, identity, version)
            result = self._decode(row)
            if row["status"] != "disabled":
                connection.execute("UPDATE price_versions SET status='disabled',disabled_at=? WHERE entry_id=? AND version=?", (now(), identity, version))
                result = self._decode(self._row(connection, identity, version))
            self._remember(connection, request_id, "disable", signature, result)
            return result

    def match(self, context):
        """Return dated, exact-condition candidates. Empty/ambiguous results never imply zero."""
        require(isinstance(context, dict), "match context must be an object")
        values, missing, invalid = {}, set(), set()
        unknown = bool(set(context).difference(CONTEXT_FIELDS))
        for name in ("material_grade", "material_form", "product_family", "thickness", "quantity"):
            value = context.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.add(name)
                continue
            try:
                values[name] = (decimal(value, name, positive=True) if name == "thickness"
                                else positive_integer(value, name) if name == "quantity"
                                else text(value, name))
            except PriceCatalogError:
                invalid.add(name)
        route = context.get("process_route")
        if route is None or route == []:
            missing.add("process_route")
            route = []
        elif not isinstance(route, list) or not route:
            invalid.add("process_route")
            route = []
        else:
            try:
                route = [text(item, "process_route item") for item in route]
                require(len(set(route)) == len(route), "duplicate process_route")
            except PriceCatalogError:
                invalid.add("process_route")
                route = []
        try:
            when = iso_date(context["as_of"], "as_of") if "as_of" in context else datetime.now(timezone(timedelta(hours=8))).date()
        except PriceCatalogError:
            when = None
        with self._connection() as connection:
            published = [self._decode(row) for row in connection.execute("SELECT * FROM price_versions WHERE status='published' ORDER BY entry_id,version").fetchall()]
        queries = [("material", None), *[("operation", operation) for operation in route], ("setup", None)]
        if not route:
            queries.insert(1, ("operation", None))
        results = []
        for kind, operation in queries:
            needed = ["material_grade", "material_form", "thickness", "quantity"]
            if kind in {"operation", "setup"}:
                needed.append("product_family")
            if kind == "operation":
                needed.append("process_route")
            result = {"kind": kind, "status": "not_found", "candidates": [], "missing": [], "requires_confirmation": True}
            if operation is not None:
                result["operation"] = operation
            result["missing"] = [name for name in needed if name in missing]
            if unknown or when is None or any(name in invalid for name in needed):
                result["status"] = "unsupported"
            elif result["missing"]:
                result["status"] = "missing_conditions"
            else:
                matching = []
                for item in published:
                    if item["kind"] != kind or item["material_grade"] != values["material_grade"] or item["material_form"] != values["material_form"]:
                        continue
                    if kind in {"operation", "setup"} and item["product_family"] != values["product_family"]:
                        continue
                    if kind == "operation" and item["operation"] != operation:
                        continue
                    if not Decimal(item["thickness_min"]) <= Decimal(values["thickness"]) <= Decimal(item["thickness_max"]):
                        continue
                    if values["quantity"] < item["qty_min"] or (item["qty_max"] is not None and values["quantity"] > item["qty_max"]):
                        continue
                    matching.append(item)
                current = [item for item in matching if item["valid_from"] <= when.isoformat() <= item["valid_to"]]
                if current:
                    result["status"] = "unique" if len(current) == 1 else "ambiguous"
                    result["candidates"] = current
                elif any(item["valid_to"] < when.isoformat() for item in matching):
                    result["status"] = "expired"
            results.append(result)
        return {"as_of": when.isoformat() if when else None, "results": results}
