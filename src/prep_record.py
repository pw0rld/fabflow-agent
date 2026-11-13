#!/usr/bin/env python3
"""Four-step inquiry records. Standard library only; never calculates prices."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

VERSION = "0.1.0"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/required-fields.json"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
STATUSES = {"missing", "unreadable", "pending_review", "verified", "conflict"}
OPERATIONS = {"record_reading", "apply_customer_reply", "apply_technical_input"}
RATE_UNITS = {"CNY/piece", "CNY/hour", "CNY/m", "CNY/hole", "CNY/bend", "CNY/order"}
DECIMAL_RAW_UNIT_ALIASES = {
    "kg/piece": {"kg/piece", "kg/件", "千克/件"},
    "CNY/kg": {"CNY/kg", "元/kg", "元/千克"},
    "CNY/order": {"CNY/order", "元/单", "元/订单"},
}


class RecordError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RecordError(message)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def number(value):
    require(isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool), "numeric value required")
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise RecordError("invalid decimal") from exc
    require(n.is_finite() and abs(n) < Decimal("1e15"), "finite reasonable numeric value required")
    return n


def length_raw_number(name, value):
    """Read a length magnitude without changing the original drawing value."""
    if name == "inner_bend_radius" and isinstance(value, str):
        radius = re.fullmatch(r"R[ \t]*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value.strip())
        if radius:
            return number(radius.group(1))
    if name == "thickness" and isinstance(value, str):
        thickness = re.fullmatch(r"[Tt][ \t]*=[ \t]*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)",
                                 value.strip(" \t"))
        if thickness:
            return number(thickness.group(1))
    return number(value)


def decimal_text(value):
    return format(number(value).normalize(), "f")


def atomic_write(path, text):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identifier(value):
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value), "invalid identifier")
    return value


def only_keys(value, allowed, label):
    require(isinstance(value, dict), f"{label} must be an object")
    require(not set(value).difference(allowed), f"unsupported {label} keys: {sorted(set(value).difference(allowed))}")


class RecordStore:
    def __init__(self, root, config_path=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = json.loads(Path(config_path or DEFAULT_CONFIG).read_text(encoding="utf-8"))
        self.locks = self.root / ".locks"
        require(not self.locks.is_symlink(), "symlink lock directory prohibited")
        self.locks.mkdir(exist_ok=True)

    def task_path(self, task_id):
        path = self.root / identifier(task_id)
        require(not path.is_symlink(), "symlink task prohibited")
        return path

    @contextmanager
    def lock(self, task_id):
        self.task_path(task_id)
        path = self.locks / (task_id + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _load(self, task_id):
        path = self.task_path(task_id) / "record.json"
        require(path.is_file() and not path.is_symlink(), "task record not found")
        record = json.loads(path.read_text(encoding="utf-8"))
        require(record["task_id"] == task_id, "task identity mismatch")
        self._check_sources(record)
        return record

    def _check_sources(self, record):
        task = self.task_path(record["task_id"])
        for source in record["sources"].values():
            path = task / source["path"]
            require(path.resolve().is_relative_to(task.resolve()), "source outside task")
            require(path.is_file() and not path.is_symlink(), "source snapshot missing")
            require(hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"], "source snapshot changed")

    def load(self, task_id):
        with self.lock(task_id):
            return self._load(task_id)

    def _source(self, record, spec, default_kind=None):
        only_keys(spec, {"id", "path", "kind", "actor", "simulated"}, "source")
        source_id = identifier(spec.get("id"))
        require(source_id not in record["sources"], "source id already exists")
        kind = spec.get("kind", default_kind)
        require(kind in {"drawing", "customer_reply", "technical_input", "operator_review"}, "invalid source kind")
        require(type(spec.get("simulated", record["simulated"])) is bool, "source simulated must be boolean")
        if kind != "drawing":
            require(isinstance(spec.get("actor"), str) and bool(spec["actor"].strip()), "human source actor required")
        path = Path(spec.get("path", "")).resolve()
        require(path.is_file(), "source file not found")
        content = path.read_bytes()
        require(len(content) <= 100 * 1024 * 1024, "source larger than 100 MiB")
        sha = hashlib.sha256(content).hexdigest()
        relative = Path("inputs") / (sha + path.suffix.lower()[:12])
        destination = self.task_path(record["task_id"]) / relative
        if not destination.exists():
            fd, temporary = tempfile.mkstemp(prefix=".input-", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        record["sources"][source_id] = {
            "id": source_id, "path": str(relative), "original_name": path.name,
            "sha256": sha, "size": len(content), "kind": kind,
            "actor": spec.get("actor"), "simulated": spec.get("simulated", record["simulated"]),
        }

    def init(self, task_id, payload):
        only_keys(payload, {"inquiry", "simulated", "files", "versions"}, "init")
        require(isinstance(payload.get("inquiry"), str) and bool(payload["inquiry"].strip()), "inquiry text required")
        require(type(payload.get("simulated")) is bool, "explicit simulated boolean required")
        require(isinstance(payload.get("files", []), list), "files must be a list")
        require(isinstance(payload.get("versions", {}), dict), "versions must be an object")
        with self.lock(task_id):
            path = self.task_path(task_id)
            require(not path.exists(), "task already exists; use a new task id")
            path.mkdir(mode=0o700)
            (path / "inputs").mkdir(mode=0o700)
            record = {
                "schema_version": 1, "module_version": VERSION, "task_id": task_id,
                "data_revision": 0, "created_at": stamp(), "updated_at": stamp(),
                "simulated": payload["simulated"], "versions": payload.get("versions", {}),
                "inquiry": payload["inquiry"], "requirements": copy.deepcopy(self.config),
                "fields": {name: {"status": "missing", "effective": None, "history": []} for name in self.config["fields"]},
                "sources": {}, "questions": [], "notes": [], "events": [], "last_operation": "init",
            }
            try:
                for source in payload.get("files", []):
                    self._source(record, source, "drawing")
                content = payload["inquiry"].encode()
                relative = "inputs/inquiry.txt"
                atomic_write(path / relative, payload["inquiry"])
                require("inquiry" not in record["sources"], "inquiry source id is reserved")
                record["sources"]["inquiry"] = {"id": "inquiry", "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(), "kind": "inquiry",
                    "actor": None, "simulated": record["simulated"], "size": len(content)}
                self._save(record)
            except Exception:
                # No committed record: remove only this newly created task's snapshots.
                if not (path / "record.json").exists():
                    import shutil
                    shutil.rmtree(path)
                raise
            return record

    def _candidate(self, record, operation, field, event_id):
        only_keys(field, {"name", "raw_value", "raw_unit", "value", "unit", "status", "evidence", "reason"}, "field")
        name = field.get("name")
        require(name in record["fields"], f"unsupported field: {name}")
        status = field.get("status")
        require(status in STATUSES, "invalid field status")
        candidate = {key: copy.deepcopy(field.get(key)) for key in ("raw_value", "raw_unit", "value", "unit", "evidence", "reason")}
        candidate.update(status=status, event_id=event_id, revision=record["data_revision"] + 1)
        require(candidate["reason"] is None or (isinstance(candidate["reason"], str) and bool(candidate["reason"].strip())), "reason must be nonempty text when supplied")
        evidence = candidate["evidence"]
        if status in {"missing", "unreadable"}:
            require(candidate["value"] is None and candidate["raw_value"] is None, "missing/unreadable values must be null")
            if status == "missing" and evidence is None:
                require(operation == "record_reading", "human supplement requires a registered source even for missing values")
                return candidate
        else:
            require(candidate["value"] is not None and candidate["raw_value"] is not None, "provided value and raw_value required")
        only_keys(evidence, {"source_type", "reference", "page", "region", "actor", "simulated"}, "evidence")
        reference = evidence.get("reference")
        require(reference in record["sources"], "evidence must reference a registered source")
        source = record["sources"][reference]
        require(evidence.get("source_type") == source["kind"], "source_type mismatch")
        require(type(evidence.get("simulated")) is bool and evidence["simulated"] == source["simulated"], "simulation provenance mismatch")
        require(evidence.get("actor") == source["actor"], "actor must match registered source")
        if evidence.get("page") is not None:
            require(type(evidence["page"]) is int and evidence["page"] > 0, "page must be positive integer")
        if source["kind"] == "drawing" and status not in {"missing", "unreadable"}:
            require(evidence.get("page") is not None and bool(evidence.get("region")), "drawing page and region required")
        allowed_sources = {
            "record_reading": {"drawing", "inquiry"},
            "apply_customer_reply": {"customer_reply", "operator_review"},
            "apply_technical_input": {"technical_input", "operator_review"},
        }
        require(source["kind"] in allowed_sources[operation], "source kind not allowed in this operation")
        if source["kind"] == "operator_review":
            require(bool(candidate["reason"]), "operator review reason required")
        require(operation != "record_reading" or status != "verified", "model reading cannot claim human verification")
        if status in {"missing", "unreadable"}:
            return candidate
        definition = record["requirements"]["fields"][name]
        kind = definition["kind"]
        value = candidate["value"]
        unit = candidate["unit"]
        raw = candidate["raw_value"]
        raw_unit = candidate["raw_unit"]
        if kind == "text":
            require(isinstance(value, str) and bool(value.strip()) and isinstance(raw, str) and bool(raw.strip()), "nonempty text required")
            require(unit is None and raw_unit is None, "text fields do not have numeric units")
        elif kind == "quantity":
            require(type(value) is int and value > 0, "quantity must be a positive integer, not page/file count")
            require(number(raw) == Decimal(value), "quantity raw value mismatch")
            require(unit == "piece" and raw_unit in {"piece", "件", "个"}, "quantity unit required")
        elif kind == "length":
            factors = {"in": Decimal("25.4"), "inch": Decimal("25.4"), "mm": Decimal(1)}
            require(raw_unit in factors and unit == "mm", "supported length units: in/inch/mm -> mm")
            expected = length_raw_number(name, raw) * factors[raw_unit]
            require(expected > 0 and number(value) == expected, "length normalization mismatch")
            candidate["value"] = decimal_text(expected)
            candidate["normalization"] = {"from": raw_unit, "to": "mm", "factor": str(factors[raw_unit])}
        elif kind == "decimal":
            expected_unit = definition["unit"]
            allowed_raw_units = DECIMAL_RAW_UNIT_ALIASES.get(expected_unit, (expected_unit,))
            require(unit == expected_unit and isinstance(raw_unit, str) and raw_unit in allowed_raw_units,
                    f"{name}: fee/usage basis must match configured unit; "
                    f"expected unit={expected_unit!r}; actual unit={unit!r}, raw_unit={raw_unit!r}")
            n = number(value)
            require(n == number(raw), "raw numeric value mismatch")
            require(n > 0 if name == "material_usage_per_piece" else n >= 0, "invalid fee/usage value")
            candidate["value"] = decimal_text(n)
        elif kind == "list":
            require(isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value), "list of nonempty strings required")
            require(not definition["required"] or bool(value), "required list is empty")
            require(value == raw and raw_unit is None and unit is None, "list raw value or units mismatch")
        elif kind == "rates":
            require(isinstance(value, list) and bool(value) and value == raw, "nonempty raw rate list required")
            require(unit is None and raw_unit is None, "rates have per-item bases")
            normalized = []
            for rate in value:
                only_keys(rate, {"operation", "value", "unit"}, "rate")
                require(isinstance(rate.get("operation"), str) and bool(rate["operation"].strip()), "operation name required")
                require(rate.get("unit") in RATE_UNITS and number(rate.get("value")) >= 0, "rate basis/nonnegative rate required")
                normalized.append({**rate, "value": decimal_text(rate["value"])})
            candidate["value"] = normalized
        return candidate

    def apply(self, task_id, operation, payload, expected_revision, event_id):
        require(operation in OPERATIONS, "unsupported operation")
        identifier(event_id)
        only_keys(payload, {"sources", "fields", "questions", "notes"}, "event")
        require(type(expected_revision) is int and expected_revision >= 0, "expected revision must be nonnegative integer")
        for key in ("sources", "fields", "questions", "notes"):
            require(isinstance(payload.get(key, []), list), f"{key} must be a list")
        signature = digest({"operation": operation, "payload": payload})
        with self.lock(task_id):
            record = self._load(task_id)
            previous = next((e for e in record["events"] if e["id"] == event_id), None)
            if previous:
                require(previous["payload_sha256"] == signature, "event id reused with different payload")
                return record
            require(expected_revision == record["data_revision"], "stale revision; reload task before supplementing")
            for source in payload.get("sources", []):
                self._source(record, source)
            seen = set()
            for field in payload.get("fields", []):
                name = field.get("name") if isinstance(field, dict) else None
                require(name not in seen, "duplicate field in event")
                seen.add(name)
                candidate = self._candidate(record, operation, field, event_id)
                slot = record["fields"][name]
                current = slot["effective"]
                different = current is not None and (current["value"] != candidate["value"] or current["unit"] != candidate["unit"])
                absent_rereading = (operation == "record_reading" and current is not None and
                                    current["status"] == "verified" and candidate["status"] in {"missing", "unreadable"})
                if different and not absent_rereading and (not candidate["reason"] or (operation == "record_reading" and current["status"] == "verified")):
                    candidate["status"] = "conflict"
                slot["history"].append(candidate)
                if candidate["status"] == "conflict":
                    slot["status"] = "conflict"
                elif current is not None and current["status"] == "verified" and operation == "record_reading":
                    # A new model candidate is not a new human decision. Keep the
                    # confirmed value/status; a differing unreasoned value above conflicts.
                    if different and not absent_rereading:
                        candidate["status"] = "conflict"
                        slot["status"] = "conflict"
                else:
                    slot["effective"] = candidate
                    slot["status"] = candidate["status"]
                if slot["status"] == "verified" and candidate["status"] == "verified":
                    for question in record["questions"]:
                        if question["field"] == name and question["status"] == "open":
                            question["status"] = "resolved"
            for question in payload.get("questions", []):
                only_keys(question, {"id", "field", "audience", "text", "status"}, "question")
                identifier(question.get("id"))
                require(question.get("field") in record["fields"], "question field not found")
                require(question.get("audience") in {"customer", "technical"}, "invalid question audience")
                require(isinstance(question.get("text"), str) and bool(question["text"].strip()), "question text required")
                require(question.get("status") in {"open", "resolved"}, "invalid question status")
                previous_question = next((q for q in record["questions"] if q["id"] == question["id"]), None)
                require(previous_question is None or previous_question["field"] == question["field"], "question id belongs to another field")
                if question["status"] == "resolved":
                    require(record["fields"][question["field"]]["status"] == "verified", "unverified question cannot be resolved")
                    require(operation != "record_reading" and question["field"] in seen and
                            record["fields"][question["field"]]["history"][-1]["status"] == "verified",
                            "resolving a question requires this event's human-sourced verification")
                record["questions"] = [q for q in record["questions"] if q["id"] != question["id"]] + [copy.deepcopy(question)]
            for note in payload.get("notes", []):
                require(isinstance(note, str) and bool(note.strip()), "note must be nonempty text")
                if note not in record["notes"]:
                    record["notes"].append(note)
            record["data_revision"] += 1
            record["updated_at"] = stamp()
            record["last_operation"] = operation
            record["simulated"] = record["simulated"] or any(s["simulated"] for s in record["sources"].values())
            record["events"].append({"id": event_id, "operation": operation, "payload_sha256": signature,
                                     "revision": record["data_revision"], "at": record["updated_at"], "payload": copy.deepcopy(payload)})
            self._save(record)
            return record

    def _validation(self, record):
        issues = []
        for name, definition in record["requirements"]["fields"].items():
            slot = record["fields"][name]
            if slot["status"] == "conflict" or (definition["required"] and slot["status"] != "verified"):
                issues.append({"field": name, "code": slot["status"], "audience": definition["stage"],
                               "message": definition["label"] + "：" + slot["status"]})
        open_questions = [q for q in record["questions"] if q["status"] != "resolved"]
        technical_seen = any(e["operation"] == "apply_technical_input" for e in record["events"])
        ready = not issues and not open_questions and technical_seen
        if ready:
            status, step = "资料已整理，待后续核算", 4
        elif record["last_operation"] == "init":
            status, step = "资料已接收，待读图", 1
        elif any(i["audience"] == "customer" for i in issues) or any(q["audience"] == "customer" for q in open_questions):
            status, step = "待客户补充或核对", 3
        else:
            status, step = "技术补录待完成", 4
        return {"task_id": record["task_id"], "data_revision": record["data_revision"],
                "status": status, "step": step, "ready": ready, "issues": issues,
                "open_questions": open_questions, "simulated": record["simulated"]}

    def validate(self, task_id):
        return self._validation(self.load(task_id))

    @staticmethod
    def _cell(value):
        text = "未提供" if value is None else (encoded(value) if isinstance(value, (list, dict)) else str(value))
        return text.replace("|", "\\|").replace("\n", "<br>")

    def _markdown(self, record):
        validation = self._validation(record)
        lines = ["# 询价参数记录", "", f"任务：{record['task_id']} · 修订：{record['data_revision']}", "",
                 f"状态：{validation['status']}", "",
                 "数据性质：" + ("包含模拟输入；工程未验证事项见下文。" if record["simulated"] else "当前任务输入；核对状态逐项列出。"), "",
                 "| 字段 | 原始值 | 原单位 | 采用值 | 单位 | 状态 | 来源/位置 |", "|---|---|---|---|---|---|---|"]
        for name, slot in record["fields"].items():
            item = slot["effective"] or {}
            evidence = item.get("evidence") or {}
            reference = " / ".join(str(evidence[k]) for k in ("source_type", "reference", "page", "region", "actor") if evidence.get(k) is not None)
            if evidence.get("simulated"):
                reference += " [模拟]"
            cells = [record["requirements"]["fields"][name]["label"], item.get("raw_value"), item.get("raw_unit"),
                     item.get("value"), item.get("unit"), slot["status"], reference or None]
            lines.append("| " + " | ".join(self._cell(x) for x in cells) + " |")
        lines += ["", "## 待补充/核对", ""]
        lines += ["- " + issue["message"] for issue in validation["issues"]] or ["- 配置要求的输入已整理。"]
        lines += ["- " + q["text"] for q in validation["open_questions"]]
        for name, slot in record["fields"].items():
            if slot["status"] == "conflict":
                lines += ["- 冲突候选 " + name + "：" + self._cell(slot["history"][-1])]
        lines += ["", "## 备注", ""] + ["- " + self._cell(note) for note in record["notes"]]
        lines += ["", "本记录到技术补录结束。原始费率仅作输入记录，后续核算另行处理。", ""]
        return "\n".join(lines)

    def _save(self, record):
        path = self.task_path(record["task_id"])
        atomic_write(path / "record.json", json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        # JSON is authoritative. render regenerates Markdown after an interrupted second write.
        atomic_write(path / "record.md", self._markdown(record))

    def render(self, task_id):
        with self.lock(task_id):
            record = self._load(task_id)
            path = self.task_path(task_id)
            atomic_write(path / "record.md", self._markdown(record))
            return {"record_json": str(path / "record.json"), "record_markdown": str(path / "record.md"),
                    "validation": self._validation(record)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Task storage directory")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("operation", choices=["init", *sorted(OPERATIONS), "validate", "render", "show"])
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--event-id")
    args = parser.parse_args(argv)
    try:
        store = RecordStore(args.root, args.config)
        if args.operation in {"validate", "render", "show"}:
            result = getattr(store, "load" if args.operation == "show" else args.operation)(args.task_id)
        else:
            require(args.input is not None, "--input required")
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            result = store.init(args.task_id, payload) if args.operation == "init" else store.apply(
                args.task_id, args.operation, payload, args.expected_revision, args.event_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (RecordError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
