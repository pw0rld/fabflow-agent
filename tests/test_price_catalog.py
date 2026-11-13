"""Offline catalogue behavior tests. All prices live in temporary test databases."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from price_catalog import PriceCatalog, PriceCatalogError


class PriceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="price-catalog-test-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "catalog.sqlite3"
        self.catalog = PriceCatalog(self.path)

    @staticmethod
    def payload(kind="material", **changes):
        data = {
            "kind": kind, "name": "独立测试价目", "rate": "20.00",
            "unit": "CNY/kg" if kind == "material" else "CNY/order" if kind == "setup" else "CNY/piece",
            "material_grade": "Q235B", "material_form": "板材",
            "thickness_min": "2.00", "thickness_max": "4.00",
            "valid_from": "2026-01-01", "valid_to": "2026-12-31",
            "currency": "CNY", "tax_included": False, "freight_included": False,
            "source": "仅供离线测试的人工构造数据", "simulated": True,
            "inclusions": ["测试费率包含项；未取得工厂实际数据"],
        }
        if kind in {"operation", "setup"}:
            data["product_family"] = "测试支架"
        if kind == "operation":
            data["operation"] = "切割"
        data.update(changes)
        return data

    @staticmethod
    def context(**changes):
        data = {
            "material_grade": "Q235B", "material_form": "板材", "thickness": "3.175",
            "quantity": 10, "product_family": "测试支架", "process_route": ["切割"],
            "as_of": "2026-10-19",
        }
        data.update(changes)
        return data

    def published(self, kind="material", **changes):
        draft = self.catalog.create(self.payload(kind, **changes))
        return self.catalog.publish(draft["entry_id"], draft["version"])

    def result(self, kind="material", **context):
        return next(item for item in self.catalog.match(self.context(**context))["results"] if item["kind"] == kind)

    def test_empty_catalogue_has_no_seed_prices(self):
        self.assertEqual(self.catalog.list_entries(), [])
        results = self.catalog.match(self.context())["results"]
        self.assertEqual([item["kind"] for item in results], ["material", "operation", "setup"])
        self.assertTrue(all(item["status"] == "not_found" and item["candidates"] == [] for item in results))

    def test_create_is_draft_and_defaults_are_explicit(self):
        result = self.catalog.create(self.payload())
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["rate"], "20")
        self.assertEqual(result["qty_min"], 1)
        self.assertIsNone(result["qty_max"])
        self.assertIsNone(result["product_family"])
        self.assertIsNone(result["operation"])
        self.assertEqual(self.result()["status"], "not_found")

    def test_published_candidate_is_a_full_snapshot_requiring_confirmation(self):
        price = self.published()
        result = self.result()
        self.assertEqual(result["status"], "unique")
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["candidates"], [price])
        for name in ("entry_id", "version", "source", "simulated", "inclusions", "valid_from", "valid_to", "snapshot_sha256"):
            self.assertIn(name, result["candidates"][0])
        self.assertNotIn("verified", price)
        self.assertNotIn("quote_total", price)

    def test_distinct_overlapping_entries_are_ambiguous(self):
        first = self.published(rate="20")
        second = self.published(rate="21")
        result = self.result()
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual({item["entry_id"] for item in result["candidates"]}, {first["entry_id"], second["entry_id"]})

    def test_conditions_are_exact_and_thickness_quantity_bounds_are_inclusive(self):
        self.published(qty_min=10, qty_max=20)
        for thickness, quantity, expected in (("2", 10, "unique"), ("4", 20, "unique"),
                                              ("1.999999999999999999999", 10, "not_found"),
                                              ("4.000000000000000000001", 10, "not_found"),
                                              ("3", 9, "not_found"), ("3", 21, "not_found")):
            with self.subTest(thickness=thickness, quantity=quantity):
                self.assertEqual(self.result(thickness=thickness, quantity=quantity)["status"], expected)
        self.assertEqual(self.result(material_grade="Q355B")["status"], "not_found")
        self.assertEqual(self.result(material_grade="q235b")["status"], "not_found")
        self.assertEqual(self.result(material_form="管材")["status"], "not_found")

    def test_each_operation_matches_exact_name_and_family(self):
        self.published("operation", operation="切割", rate="12")
        self.published("operation", operation="折弯", rate="18")
        results = self.catalog.match(self.context(process_route=["切割", "折弯", "焊接"]))["results"]
        operations = [item for item in results if item["kind"] == "operation"]
        self.assertEqual([(item["operation"], item["status"]) for item in operations],
                         [("切割", "unique"), ("折弯", "unique"), ("焊接", "not_found")])
        self.assertEqual(self.result("operation", product_family="其他产品")["status"], "not_found")
        self.assertEqual(self.result("operation", process_route=["激光下料"])["status"], "not_found")

    def test_missing_critical_parameters_never_trigger_broad_matching(self):
        for kind in ("material", "operation", "setup"):
            self.published(kind)
        for field in ("material_grade", "material_form", "thickness", "quantity"):
            with self.subTest(field=field):
                results = self.catalog.match(self.context(**{field: None}))["results"]
                self.assertTrue(all(item["status"] == "missing_conditions" for item in results))
                self.assertTrue(all(field in item["missing"] and not item["candidates"] for item in results))
        result = self.catalog.match(self.context(product_family=None))["results"]
        self.assertEqual(result[0]["status"], "unique")
        self.assertEqual(result[1]["missing"], ["product_family"])
        self.assertEqual(result[2]["missing"], ["product_family"])
        result = self.result("operation", process_route=[])
        self.assertEqual(result["status"], "missing_conditions")
        self.assertIn("process_route", result["missing"])

    def test_invalid_context_is_unsupported_not_a_guess(self):
        self.published()
        for changes in ({"quantity": True}, {"quantity": 0}, {"quantity": "10"},
                        {"thickness": 3.175}, {"thickness": "NaN"},
                        {"as_of": "2026-02-30"}, {"width": "100"}):
            with self.subTest(changes=changes):
                result = self.result(**changes)
                self.assertEqual(result["status"], "unsupported")
                self.assertEqual(result["candidates"], [])
        self.assertEqual(self.result("operation", process_route=["切割", "切割"])["status"], "unsupported")

    def test_expired_and_future_prices_are_not_selectable(self):
        self.published(valid_from="2025-01-01", valid_to="2025-12-31")
        self.assertEqual(self.result()["status"], "expired")
        self.assertEqual(self.result()["candidates"], [])
        self.assertEqual(self.result(material_form="管材")["status"], "not_found")
        self.published(valid_from="2027-01-01", valid_to="2027-12-31")
        self.assertEqual(self.result(as_of="2024-12-31")["status"], "not_found")

    def test_validity_dates_are_inclusive(self):
        self.published(valid_from="2026-10-19", valid_to="2026-10-19")
        self.assertEqual(self.result(as_of="2026-10-19")["status"], "unique")
        self.assertEqual(self.result(as_of="2026-10-18")["status"], "not_found")
        self.assertEqual(self.result(as_of="2026-10-20")["status"], "expired")

    def test_new_draft_does_not_replace_published_until_publish(self):
        original = self.published(rate="20")
        draft = self.catalog.new_version(original["entry_id"], self.payload(rate="25"))
        self.assertEqual(draft["version"], 2)
        self.assertEqual(self.result()["candidates"][0]["rate"], "20")
        latest = self.catalog.publish(original["entry_id"], 2)
        old = self.catalog.get(original["entry_id"], 1)
        self.assertEqual(old["status"], "disabled")
        self.assertEqual(old["rate"], "20")
        self.assertEqual(old["snapshot_sha256"], original["snapshot_sha256"])
        self.assertEqual(old["published_at"], original["published_at"])
        self.assertEqual(self.result()["candidates"], [latest])
        self.catalog.disable(original["entry_id"], 2)
        self.assertEqual(self.result()["status"], "not_found")
        self.assertEqual(self.catalog.get(original["entry_id"], 1)["status"], "disabled")

    def test_new_future_version_does_not_silently_fall_back_to_old_price(self):
        original = self.published()
        self.catalog.new_version(original["entry_id"], self.payload(valid_from="2027-01-01", valid_to="2027-12-31"))
        self.catalog.publish(original["entry_id"], 2)
        self.assertEqual(self.result()["status"], "not_found")

    def test_disabled_and_older_versions_cannot_be_republished(self):
        original = self.published()
        self.catalog.new_version(original["entry_id"], self.payload(rate="21"))
        self.catalog.new_version(original["entry_id"], self.payload(rate="22"))
        self.catalog.publish(original["entry_id"], 3)
        for version in (1, 2):
            with self.subTest(version=version), self.assertRaises(PriceCatalogError):
                self.catalog.publish(original["entry_id"], version)
        self.catalog.disable(original["entry_id"], 3)
        with self.assertRaises(PriceCatalogError):
            self.catalog.publish(original["entry_id"], 3)

    def test_price_content_and_history_have_database_immutability_guards(self):
        price = self.published()
        with sqlite3.connect(self.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE price_versions SET snapshot_json='{}' WHERE entry_id=?", (price["entry_id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM price_versions WHERE entry_id=?", (price["entry_id"],))
        self.assertEqual(self.catalog.get(price["entry_id"], 1), price)
        copied = self.catalog.get(price["entry_id"], 1)
        copied["inclusions"].append("caller mutation")
        self.assertEqual(self.catalog.get(price["entry_id"], 1), price)

    def test_reopening_database_preserves_history_and_candidates(self):
        price = self.published("operation")
        reopened = PriceCatalog(self.path)
        self.assertEqual(reopened.get(price["entry_id"], 1), price)
        self.assertEqual(reopened.match(self.context()), self.catalog.match(self.context()))

    def test_first_use_initialization_is_safe_across_catalogue_instances(self):
        path = Path(self.temporary.name) / "concurrent-new.sqlite3"
        barrier = threading.Barrier(6)
        def initialize(_):
            barrier.wait(timeout=10)
            catalogue = PriceCatalog(path)
            return catalogue.create(self.payload(), request_id="first-create")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(initialize, range(6)))
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(len(PriceCatalog(path).list_entries()), 1)

    def test_first_use_retries_immediate_busy_during_wal_switch(self):
        attempts = []
        original_connect = sqlite3.connect
        class BusyOnceConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "PRAGMA journal_mode = WAL":
                    attempts.append(sql)
                    if len(attempts) == 1:
                        error = sqlite3.OperationalError("database is locked")
                        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                        raise error
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            return original_connect(*args, factory=BusyOnceConnection, **kwargs)
        with patch("price_catalog.sqlite3.connect", side_effect=connect):
            catalogue = PriceCatalog(Path(self.temporary.name) / "busy-first-use.sqlite3")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(catalogue.list_entries(), [])

    def test_wal_initialization_does_not_retry_non_lock_errors(self):
        attempts = []
        original_connect = sqlite3.connect
        class DeniedConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "PRAGMA journal_mode = WAL":
                    attempts.append(sql)
                    error = sqlite3.OperationalError("disk I/O error")
                    error.sqlite_errorcode = sqlite3.SQLITE_IOERR
                    raise error
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            return original_connect(*args, factory=DeniedConnection, **kwargs)
        with patch("price_catalog.sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
                PriceCatalog(Path(self.temporary.name) / "bad-first-use.sqlite3")
        self.assertEqual(len(attempts), 1)

    def test_entry_arguments_support_the_documented_keyword_names(self):
        original = self.catalog.create(self.payload())
        identity = original["entry_id"]
        self.assertEqual(self.catalog.get(entry_id=identity, version=1), original)
        self.catalog.publish(entry_id=identity, version=1)
        newer = self.catalog.new_version(entry_id=identity, payload=self.payload(rate="21"))
        self.assertEqual(newer["version"], 2)
        self.assertEqual(self.catalog.disable(entry_id=identity, version=1)["status"], "disabled")

    def test_invalid_payloads_do_not_create_rows(self):
        bad = [
            {"rate": 20}, {"rate": "NaN"}, {"rate": "1e3"}, {"rate": "-1"},
            {"thickness_min": "0"}, {"thickness_min": "4.1"}, {"thickness_max": 4},
            {"qty_min": 0}, {"qty_min": True}, {"qty_min": 10, "qty_max": 9},
            {"qty_max": "20"}, {"valid_from": "2026-02-30"},
            {"valid_from": "2027-01-01"}, {"valid_to": None},
            {"currency": "USD"}, {"tax_included": True}, {"freight_included": 0},
            {"source": " "}, {"simulated": "true"}, {"inclusions": []},
            {"inclusions": [""]}, {"material_grade": None}, {"material_form": ""},
            {"unit": "元/kg"}, {"unit": "CNY/piece"}, {"product_family": "not-a-material-condition"},
            {"unrecognized": "must not be ignored"},
        ]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(PriceCatalogError):
                self.catalog.create(self.payload(**changes))
        for missing in ("source", "simulated", "valid_from", "valid_to", "thickness_min", "tax_included"):
            data = self.payload()
            data.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(PriceCatalogError):
                self.catalog.create(data)
        self.assertEqual(self.catalog.list_entries(), [])

    def test_kind_specific_conditions_and_supported_units(self):
        for kind, changes in (("operation", {"product_family": ""}),
                              ("operation", {"operation": None}),
                              ("operation", {"unit": "CNY/hour"}),
                              ("setup", {"product_family": None}),
                              ("setup", {"operation": "切割"}),
                              ("setup", {"unit": "CNY/piece"})):
            with self.subTest(kind=kind, changes=changes), self.assertRaises(PriceCatalogError):
                self.catalog.create(self.payload(kind, **changes))
        for kind, unit in (("material", "CNY/kg"), ("operation", "CNY/piece"),
                           ("operation", "CNY/order"), ("setup", "CNY/order")):
            record = self.catalog.create(self.payload(kind, unit=unit))
            self.assertEqual(record["unit"], unit)

    def test_zero_price_is_explicit_data_not_a_missing_default(self):
        price = self.published(rate="0.00")
        self.assertEqual(price["rate"], "0")
        self.assertEqual(self.result()["candidates"][0]["rate"], "0")
        data = self.payload()
        data.pop("rate")
        with self.assertRaises(PriceCatalogError):
            self.catalog.create(data)

    def test_new_version_requires_full_payload_and_same_kind(self):
        original = self.catalog.create(self.payload())
        with self.assertRaises(PriceCatalogError):
            self.catalog.new_version(original["entry_id"], {"rate": "21"})
        with self.assertRaises(PriceCatalogError):
            self.catalog.new_version(original["entry_id"], self.payload("setup"))
        self.assertEqual(len(self.catalog.list_entries()), 1)

    def test_untrusted_text_is_parameterized_and_matches_only_itself(self):
        grade = "Q235B' OR 1=1; DROP TABLE price_versions; --"
        price = self.published(material_grade=grade)
        self.assertEqual(self.result()["status"], "not_found")
        self.assertEqual(self.result(material_grade=grade)["candidates"], [price])
        with self.assertRaises(PriceCatalogError):
            self.catalog.get("x' OR 1=1 --", 1)
        self.assertEqual(len(self.catalog.list_entries()), 1)

    def test_request_id_makes_create_and_new_version_idempotent(self):
        original = self.catalog.create(self.payload(), request_id="create-once")
        self.assertEqual(self.catalog.create(self.payload(), request_id="create-once"), original)
        next_version = self.catalog.new_version(original["entry_id"], self.payload(rate="21"), request_id="version-once")
        self.assertEqual(self.catalog.new_version(original["entry_id"], self.payload(rate="21"), request_id="version-once"), next_version)
        self.assertEqual(len(self.catalog.list_entries()), 2)
        with self.assertRaises(PriceCatalogError):
            self.catalog.create(self.payload(rate="22"), request_id="create-once")
        with self.assertRaises(PriceCatalogError):
            self.catalog.new_version(original["entry_id"], self.payload(rate="23"), request_id="version-once")

    def test_publication_receipt_replay_does_not_resurrect_disabled_version(self):
        original = self.catalog.create(self.payload(), request_id="create")
        first = self.catalog.publish(original["entry_id"], 1, request_id="publish")
        stopped = self.catalog.disable(original["entry_id"], 1, request_id="disable")
        self.assertEqual(self.catalog.publish(original["entry_id"], 1, request_id="publish"), first)
        self.assertEqual(self.catalog.disable(original["entry_id"], 1, request_id="disable"), stopped)
        self.assertEqual(self.catalog.get(original["entry_id"], 1)["status"], "disabled")
        with self.assertRaises(PriceCatalogError):
            self.catalog.disable(original["entry_id"], 1, request_id="publish")

    def test_failure_before_receipt_commit_rolls_back_publication_and_retirement(self):
        original = self.published()
        draft = self.catalog.new_version(original["entry_id"], self.payload(rate="21"))
        with patch.object(self.catalog, "_remember", side_effect=RuntimeError("simulated failure before commit")):
            with self.assertRaises(RuntimeError):
                self.catalog.publish(original["entry_id"], 2, request_id="atomic-publication")
        self.assertEqual(self.catalog.get(original["entry_id"], 1), original)
        self.assertEqual(self.catalog.get(original["entry_id"], 2), draft)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_requests WHERE request_id=?", ("atomic-publication",)).fetchone()[0], 0)
        self.catalog.publish(original["entry_id"], 2, request_id="atomic-publication")
        self.assertEqual(self.result()["candidates"][0]["version"], 2)

    def test_concurrent_same_request_creates_only_one_entry(self):
        barrier = threading.Barrier(6)
        def create_once(_):
            barrier.wait(timeout=10)
            return self.catalog.create(self.payload(), request_id="same-concurrent-request")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(create_once, range(6)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.catalog.list_entries()), 1)

    def test_concurrent_new_versions_have_distinct_monotonic_numbers(self):
        original = self.catalog.create(self.payload())
        barrier = threading.Barrier(6)
        def make_version(index):
            barrier.wait(timeout=10)
            return self.catalog.new_version(original["entry_id"], self.payload(rate=str(index + 30)), request_id=f"version-{index}")
        with ThreadPoolExecutor(max_workers=6) as pool:
            versions = list(pool.map(make_version, range(6)))
        self.assertEqual(sorted(item["version"] for item in versions), list(range(2, 8)))

    def test_concurrent_publication_never_reverts_to_older_price(self):
        original = self.published()
        self.catalog.new_version(original["entry_id"], self.payload(rate="21"))
        self.catalog.new_version(original["entry_id"], self.payload(rate="22"))
        barrier = threading.Barrier(2)
        def publish_version(version):
            barrier.wait(timeout=10)
            try:
                return self.catalog.publish(original["entry_id"], version, request_id=f"publish-{version}")
            except PriceCatalogError as error:
                self.assertEqual(error.code, "conflict")
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(publish_version, (2, 3)))
        active = [item for item in self.catalog.list_entries() if item["status"] == "published"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["version"], 3)
        self.assertEqual(self.result()["candidates"][0]["rate"], "22")
        self.assertEqual(self.catalog.get(original["entry_id"], 1)["snapshot_sha256"], original["snapshot_sha256"])


if __name__ == "__main__":
    unittest.main()
