"""Offline bridge contract tests: fake rc2 RPC only, never a model request."""
from copy import deepcopy
from http.client import IncompleteRead
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dsh_bridge import DshBridge, DshBridgeError, GAUGE_WARNING, MANIFEST, MAX_OBSERVATIONS, PROCESS_ROUTE_WARNING, READER_INSTRUCTIONS
from recognition_view import RECOGNITION_FIELDS


def event(seq, kind, turn=1, **data):
    return {"event": {"seq": seq, "type": kind, "data": {"turn": turn, **data}}}


def timed_event(seq, kind, at, turn=1, **data):
    result = event(seq, kind, turn=turn, **data)
    result["event"]["time"] = at
    return result


def candidate(name="thickness", raw="0.125", value="3.175", raw_unit="in", unit="mm", **extra):
    return {"name": name, "raw_value": raw, "raw_unit": raw_unit, "value": value,
            "unit": unit, "evidence": {"reference": "drawing-1", "page": 1, "region": "尺寸 .125"},
            "reason": "按原图单位准确换算", **extra}


def observation(name="thickness", raw_text="(.060)", raw_unit=None, status="unit_unknown", **extra):
    return {"name": name, "raw_text": raw_text, "raw_unit": raw_unit, "status": status,
            "evidence": {"reference": "drawing-1", "page": 1, "region": "侧视图括号尺寸"},
            "reason": "未找到明确的尺寸单位", **extra}


def drawing_part(key="part-01", name="支架", part_number="01", version="04", indices=None, evidence=None):
    return {"key": key, "name": name, "part_number": part_number, "version": version,
            "observation_indices": [0] if indices is None else indices,
            "evidence": [{"reference": "drawing-1", "page": 1, "region": "标题栏"}] if evidence is None else evidence}


class FakeBridge(DshBridge):
    def __init__(self, workspace):
        super().__init__("http://127.0.0.1:3080", workspace)
        self.calls = []
        self.events = []
        self.running = True
        self.failures = {}

    def _rpc(self, method, payload, **kwargs):
        self.calls.append((method, deepcopy(payload), kwargs))
        if method in self.failures:
            raise self.failures[method]
        if method == "session.create":
            self.sid = payload["sessionId"]
            return {"sessionId": self.sid}
        if method in {"session.prompt", "session.cancel"}:
            return {"accepted": True}
        if method == "session.history":
            return {"events": deepcopy(self.events), "hasMore": False}
        if method == "session.list":
            return {"items": [{"sessionId": self.sid, "running": self.running}]}
        raise AssertionError(method)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.file = self.workspace / "input.png"
        self.file.write_bytes(b"isolated fake drawing bytes; not real image recognition")
        self.files = [{"id": "drawing-1", "path": str(self.file), "original_name": "客户图.png"}]
        self.output = self.workspace / "candidates.json"
        self.bridge = FakeBridge(self.workspace)

    def start(self, **kwargs):
        result = self.bridge.start("TASK-1", self.files, "请读当前附件", self.output, **kwargs)
        self.sid = result["session_id"]
        return result

    def legacy_manifest(self):
        path = self.workspace / MANIFEST
        run = json.loads(path.read_text())
        run.pop("observation_version", None)
        path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n")

    def completed(self, candidates=None, text=None):
        if text is None:
            text = "```json\n" + json.dumps({"candidates": candidates if candidates is not None else [candidate()], "warnings": []}) + "\n```"
        self.bridge.events = [event(1, "turn/start"), event(2, "assistant/message", message={"content": [{"type": "text", "text": text}]}),
                              event(3, "turn/end", reason={"kind": "completed"})]
        return self.bridge.inspect(self.sid)

    def test_persists_id_before_callback_and_prompt_and_uses_isolated_cwd(self):
        def saved(sid):
            self.assertEqual(json.loads((self.workspace / MANIFEST).read_text())["session_id"], sid)
            self.assertEqual([c[0] for c in self.bridge.calls], ["session.create"])
        result = self.start(on_session=saved)
        self.assertEqual(result["status"], "running")
        create, prompt = self.bridge.calls
        self.assertEqual(create[1]["cwd"], str(self.workspace.resolve()))
        self.assertEqual(create[1]["sessionId"], result["session_id"])
        self.assertEqual(prompt[0], "session.prompt")
        self.assertEqual(prompt[1]["mode"], "queue")
        text = prompt[1]["content"][0]["text"]
        self.assertIn(str(self.file.resolve()), text)
        self.assertIn("ask_user_question", text)
        self.assertIn("不猜数量", text)
        self.assertNotIn(str(self.output), text)
        self.assertEqual((self.workspace / "AGENTS.md").read_text(), READER_INSTRUCTIONS)

    def test_prompt_timeout_retains_session_and_restarting_does_not_resubmit(self):
        self.bridge.failures["session.prompt"] = DshBridgeError("timeout", "transport-error")
        result = self.start()
        self.assertEqual(result["status"], "unknown")
        recovered = FakeBridge(self.workspace)
        again = recovered.start("TASK-1", self.files, "请读当前附件", self.output)
        self.assertEqual(again["session_id"], self.sid)
        self.assertTrue(again["requires_inspect"])
        self.assertEqual(recovered.calls, [])

    def test_callback_failure_never_sends_prompt(self):
        def failed(sid):
            raise OSError("storage unavailable")
        result = self.start(on_session=failed)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "callback-failed")
        self.assertEqual([c[0] for c in self.bridge.calls], ["session.create"])

    def test_changed_inputs_cannot_reuse_a_run(self):
        self.start()
        with self.assertRaises(ValueError):
            self.bridge.start("TASK-2", self.files, "请读当前附件", self.output)
        self.file.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.bridge.start("TASK-1", self.files, "请读当前附件", self.output)

    def test_outside_symlink_and_reserved_paths_rejected(self):
        outside = self.workspace.parent / "outside.png"
        for path in (outside, self.workspace / "AGENTS.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.bridge.start("TASK-1", self.files, "", path)
        link = self.workspace / "linked.png"
        link.symlink_to(self.file)
        with self.assertRaises(ValueError):
            self.bridge.start("TASK-1", [{"id": "drawing-1", "path": str(link)}], "", self.output)
        with self.assertRaises(ValueError):
            self.bridge.start("TASK-1", [{"id": "inquiry", "path": str(self.file)}], "", self.output)

    def test_other_session_cannot_be_inspected_or_cancelled(self):
        self.start()
        for operation in (self.bridge.inspect, self.bridge.stop):
            with self.assertRaises(ValueError):
                operation("session-unrelated")

    def test_complete_candidate_is_pending_preserves_raw_and_writes_fixed_output_once(self):
        self.start()
        result = self.completed([candidate(status="verified")])
        self.assertEqual(result["status"], "completed")
        item = result["candidates"][0]
        self.assertEqual((item["raw_value"], item["raw_unit"], item["value"], item["unit"]), ("0.125", "in", "3.175", "mm"))
        self.assertEqual(item["status"], "pending_review")
        before = self.output.read_bytes()
        self.assertEqual(self.bridge.inspect(self.sid), result)
        self.assertEqual(self.output.read_bytes(), before)
        self.output.write_text("existing unrelated result")
        rejected = self.bridge.inspect(self.sid)
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(self.output.read_text(), "existing unrelated result")

    def test_inquiry_evidence_uses_actual_inquiry_and_no_page(self):
        self.start()
        item = candidate("quantity", 10, 10, "件", "piece")
        item["evidence"] = {"reference": "inquiry", "page": None, "region": "询价原文"}
        self.assertEqual(self.completed([item])["status"], "completed")

    def test_partial_message_and_retry_do_not_become_completed(self):
        self.start()
        self.bridge.events = [event(1, "turn/start"), event(2, "assistant/chunk", chunk={"type": "text", "text": '{"candidates":[]}'}),
                              event(3, "llm/retry", error={"code": "TIMEOUT"})]
        result = self.bridge.inspect(self.sid)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["candidates"], [])
        self.assertIsNotNone(result["retry"])
        self.assertFalse(self.output.exists())
        self.bridge.running = False
        self.assertEqual(self.bridge.inspect(self.sid)["status"], "unknown")

    def test_accepted_but_not_started_session_remains_unknown_and_does_not_resubmit(self):
        self.start()
        self.bridge.running = False
        self.bridge.events = []
        result = self.bridge.inspect(self.sid)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["phase"], "awaiting_turn_end")
        self.assertEqual(result["candidates"], [])
        self.assertFalse(self.output.exists())
        again = self.bridge.start("TASK-1", self.files, "请读当前附件", self.output)
        self.assertEqual(again["session_id"], self.sid)
        self.assertEqual(sum(call[0] == "session.prompt" for call in self.bridge.calls), 1)

    def test_prior_completed_turn_does_not_hide_later_running_turn(self):
        self.start()
        self.completed([])
        self.output.unlink()
        self.bridge.events += [event(4, "turn/start", turn=2)]
        self.assertEqual(self.bridge.inspect(self.sid)["status"], "running")
        self.assertFalse(self.output.exists())

    def test_actual_terminal_reasons_and_unexpected_question(self):
        self.start()
        for kind, expected in [("aborted", "stopped"), ("blocked", "waiting"), ("error", "failed"), ("max-tokens", "failed"), ("interrupted", "failed")]:
            with self.subTest(kind=kind):
                self.bridge.events = [event(1, "turn/start"), event(2, "turn/end", reason={"kind": kind})]
                self.assertEqual(self.bridge.inspect(self.sid)["status"], expected)
        self.bridge.events = [event(1, "turn/start"), event(2, "tool/call", name="ask_user_question", callId="ask-1", arguments="{}")]
        self.assertEqual(self.bridge.inspect(self.sid)["status"], "waiting")

    def test_poll_and_stop_transport_uncertainty_remains_unknown(self):
        self.start()
        for method, operation in [("session.history", self.bridge.inspect), ("session.cancel", self.bridge.stop)]:
            for code in ("transport-error", "protocol-error"):
                with self.subTest(method=method, code=code):
                    self.bridge.failures[method] = DshBridgeError("unknown outcome", code)
                    result = operation(self.sid)
                    self.assertEqual(result["status"], "unknown")
                    self.assertEqual(result["session_id"], self.sid)
                    self.assertEqual(result["error"]["code"], code)
            self.bridge.failures.clear()

    def test_cancel_ack_is_stopping_until_abort_event(self):
        self.start()
        self.assertEqual(self.bridge.stop(self.sid)["status"], "stopping")
        self.assertEqual(self.bridge.calls[-1][0], "session.cancel")
        self.assertTrue(json.loads((self.workspace / MANIFEST).read_text())["stop_requested"])
        self.bridge.events = [event(1, "turn/end", reason={"kind": "aborted", "reason": {"kind": "user"}})]
        self.assertEqual(self.bridge.inspect(self.sid)["status"], "stopped")

    def test_attachment_tampering_prevents_result_adoption(self):
        self.start()
        self.file.write_bytes(b"different drawing")
        result = self.completed()
        self.assertEqual(result["status"], "failed")
        self.assertIn("attachment changed", result["error"]["message"])
        self.assertFalse(self.output.exists())

    def test_untrusted_json_scope_and_values_rejected(self):
        self.start()
        malformed = []
        for update in ({"name": "total_price"}, {"value": None}, {"value": "3.2"}, {"unit": "in"}, {"raw_value": "NaN"}):
            malformed.append({"candidates": [candidate(**update)]})
        bad_evidence = candidate()
        bad_evidence["evidence"]["reference"] = "other-task-drawing"
        malformed += [{"candidates": [bad_evidence]}, {"candidates": [candidate(), candidate()]},
                      {"candidates": [], "output_path": "/tmp/model-chosen.json"}]
        for payload in malformed:
            with self.subTest(payload=payload):
                result = self.completed(text=json.dumps(payload))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["candidates"], [])
                self.assertFalse(self.output.exists())

    def test_duplicate_keys_and_multiple_candidate_blocks_rejected(self):
        self.start()
        for text in ('{"candidates":[],"candidates":[]}', '```json\n{"candidates":[]}\n```\n```json\n{"candidates":[]}\n```'):
            with self.subTest(text=text):
                self.assertEqual(self.completed(text=text)["status"], "failed")

    def test_explicit_technical_rates_remain_candidates_without_a_quote(self):
        self.start()
        rates = [{"operation": "切割", "value": "2.00", "unit": "CNY/piece"}]
        result = self.completed([candidate("material_usage_per_piece", "5", "5", "kg/件", "kg/piece"),
                                 candidate("operation_rates", rates, rates, None, None)])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(all(item["status"] == "pending_review" for item in result["candidates"]))
        self.assertNotIn("amounts", result)
        self.assertFalse((self.workspace / "quotes").exists())

    def test_quantity_fee_zero_usage_and_rate_basis_remain_strict(self):
        self.start()
        rates = [{"operation": "切割", "value": "-1", "unit": "元/件"}]
        for item in (candidate("quantity", 0, 0, "件", "piece"), candidate("quantity", 1.5, 1.5, "件", "piece"),
                     candidate("material_usage_per_piece", "0", "0", "kg/件", "kg/piece"),
                     candidate("setup_fee", "1", "1", "CNY/kg", "CNY/order"),
                     candidate("operation_rates", rates, rates, None, None)):
            with self.subTest(item=item):
                self.assertEqual(self.completed([item])["status"], "failed")

    def test_boxes_are_raw_evidence_and_never_become_an_order_piece_quantity(self):
        self.start()
        quantity = candidate("quantity", "80", 80, "boxes", "piece",
                             evidence={"reference": "drawing-1", "page": 1,
                                       "region": "TOTAL: 5 part types / 8 boxes per set / 80 boxes"},
                             reason="图纸 TOTAL 标注 80 boxes")
        material = candidate("material_grade", "Q235B", "Q235B", None, None)
        for explicit in (False, True):
            with self.subTest(explicit_observations=explicit):
                payload = {"candidates": [quantity, material], "warnings": []}
                if explicit:
                    payload["observations"] = []
                text = json.dumps(payload, ensure_ascii=False)
                result = self.completed(text=text)
                self.assertEqual(result["status"], "completed")
                self.assertEqual([row["name"] for row in result["candidates"]], ["material_grade"])
                self.assertEqual(result["candidates"][0]["raw_value"], "Q235B")
                self.assertTrue(any("数量" in warning for warning in result["warnings"]))
                observations = [row for row in result["observations"] if row["name"] == "quantity"]
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]["raw_text"], "80")
                self.assertEqual(observations[0]["raw_unit"], "boxes")
                self.assertEqual(observations[0]["evidence"], quantity["evidence"])
                self.assertEqual(observations[0]["status"], "unit_unknown")
                self.assertNotIn("value", observations[0])
                self.assertEqual(json.loads(result["text"]), payload)
                before = self.output.read_bytes()
                again = self.bridge.inspect(self.sid)
                self.assertEqual(again["status"], "completed")
                self.assertEqual(again["observations"], result["observations"])
                self.assertEqual(self.output.read_bytes(), before)
                self.output.unlink()

    def test_quantity_observation_supplement_deduplicates_only_the_same_source_label(self):
        self.start()
        evidence = {"reference": "drawing-1", "page": 1, "region": "TOTAL: 80 boxes"}
        quantity = candidate("quantity", "80", 80, "boxes", "piece", evidence=evidence)
        exact = observation("quantity", "80", "boxes", "unit_unknown", evidence=evidence,
                            reason="原图箱数，尚未确认件数")
        other = observation("quantity", "8 boxes per set", "boxes", "unit_unknown",
                            evidence={**evidence, "region": "每套包装说明"},
                            reason="每套箱数不等于订单件数")
        payload = {"candidates": [quantity], "observations": [exact, other]}
        result = self.completed(text=json.dumps(payload, ensure_ascii=False))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["observations"], [exact, other])

    def test_unsupported_quantity_unit_does_not_relax_numbers_types_or_source_validation(self):
        self.start()
        quantity = candidate("quantity", "80", 80, "boxes", "piece")
        invalid = [
            {**quantity, "raw_value": "1.5", "value": 1.5},
            {**quantity, "raw_value": "0", "value": 0},
            {**quantity, "raw_value": "-1", "value": -1},
            {**quantity, "raw_value": True, "value": True},
            {**quantity, "raw_value": "80", "value": "80"},
            {**quantity, "raw_value": "80", "value": 80.0},
            {**quantity, "raw_value": "8"},
            {**quantity, "raw_value": "NaN"},
            {**quantity, "unit": "set"},
            {**quantity, "raw_unit": ["boxes"]},
            {**quantity, "evidence": {**quantity["evidence"], "reference": "other-task"}},
        ]
        for item in invalid:
            with self.subTest(item=item):
                result = self.completed(text=json.dumps({"candidates": [item, candidate()], "observations": []}))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["candidates"], [])
                self.assertFalse(self.output.exists())

    def test_text_engineering_items_keep_original_evidence_and_other_usable_fields(self):
        self.start()
        raw = "Confirm material grade; confirm whether spot welding and galvanizing are required."
        engineering = candidate("engineering_open_items", raw,
                                ["确认材料牌号", "确认点焊和镀锌要求"], None, None,
                                evidence={"reference": "drawing-1", "page": 1, "region": "工程备注"})
        for explicit in (False, True):
            with self.subTest(explicit_observations=explicit):
                payload = {"candidates": [engineering, candidate()], "warnings": []}
                if explicit:
                    payload["observations"] = []
                result = self.completed(text=json.dumps(payload, ensure_ascii=False))
                self.assertEqual(result["status"], "completed")
                self.assertEqual([row["name"] for row in result["candidates"]], ["thickness"])
                self.assertEqual(result["candidates"][0]["value"], "3.175")
                rows = [row for row in result["observations"] if row["name"] == "engineering_open_items"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["raw_text"], raw)
                self.assertIsNone(rows[0]["raw_unit"])
                self.assertEqual(rows[0]["evidence"], engineering["evidence"])
                self.assertEqual(rows[0]["status"], "observed")
                self.assertTrue(any("工程" in warning for warning in result["warnings"]))
                self.assertEqual(json.loads(result["text"]), payload)
                self.output.unlink()

    def test_text_engineering_deferral_does_not_allow_malformed_or_arbitrary_list_fields(self):
        self.start()
        engineering = candidate("engineering_open_items", "Confirm grade and surface finish",
                                ["确认牌号", "确认表面处理"], None, None)
        invalid = [
            {**engineering, "raw_value": " "},
            {**engineering, "raw_value": "x" * 10001},
            {**engineering, "value": []},
            {**engineering, "value": [""]},
            {**engineering, "value": [True]},
            {**engineering, "value": "确认牌号"},
            {**engineering, "raw_unit": "mm"},
            {**engineering, "unit": "mm"},
            {**engineering, "raw_value": {"text": "Confirm grade"}},
            {**engineering, "raw_value": ["different findings"]},
            {**engineering, "name": "fee_inclusions"},
            {**engineering, "evidence": {**engineering["evidence"], "reference": "other-task"}},
        ]
        for item in invalid:
            with self.subTest(item=item):
                self.assertEqual(self.completed([item, candidate()])["status"], "failed")
                self.assertFalse(self.output.exists())
        self.assertEqual(self.completed([engineering, engineering])["status"], "failed")
        self.assertFalse(self.output.exists())

    def test_saved_raw_quantity_and_engineering_projection_needs_no_rpc_or_file_rewrite(self):
        self.start()
        self.legacy_manifest()
        rows = [candidate("quantity", "80", 80, "boxes", "piece"),
                candidate("engineering_open_items", "Confirm manufacturing and finish requirements",
                          ["确认加工与表面处理要求"], None, None),
                candidate("material_grade", "Q235B", "Q235B", None, None)]
        text = json.dumps({"candidates": rows, "warnings": []}, ensure_ascii=False)
        before = {path.name: path.read_bytes() for path in self.workspace.iterdir() if path.is_file()}
        calls_before = deepcopy(self.bridge.calls)
        projected = self.bridge.observations_from_text(text, self.bridge._run(self.sid))
        quantity = next(row for row in projected if row["name"] == "quantity")
        engineering = next(row for row in projected if row["name"] == "engineering_open_items")
        self.assertEqual((quantity["raw_text"], quantity["raw_unit"], quantity["status"]),
                         ("80", "boxes", "unit_unknown"))
        self.assertEqual(quantity["evidence"], rows[0]["evidence"])
        self.assertEqual(engineering["raw_text"], rows[1]["raw_value"])
        self.assertEqual(engineering["status"], "observed")
        self.assertEqual(self.bridge.calls, calls_before)
        self.assertEqual({path.name: path.read_bytes() for path in self.workspace.iterdir() if path.is_file()}, before)

    def test_gauge_thickness_is_deferred_without_losing_other_sourced_candidates(self):
        self.start()
        gauge = candidate("thickness", "16 GAUGE", "16 GAUGE", "gauge", None)
        material = candidate("material_grade", "A36 STEEL", "A36 STEEL", None, None)
        result = self.completed([gauge, material])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["name"] for item in result["candidates"]], ["material_grade"])
        self.assertEqual(result["candidates"][0]["status"], "pending_review")
        self.assertIn(GAUGE_WARNING, result["warnings"])
        # The exception is only omission of gauge thickness, not evidence or
        # nonfinite-number leniency. Keep the valid artifact out of these runs.
        self.output.unlink()
        for unit in ("Gauge", "GA", "ga."):
            with self.subTest(unit=unit):
                bad = candidate("thickness", "NaN", "16", unit, None)
                self.assertEqual(self.completed([bad, material])["status"], "failed")
        bad_evidence = deepcopy(gauge)
        bad_evidence["evidence"]["reference"] = "other-task"
        self.assertEqual(self.completed([bad_evidence, material])["status"], "failed")
        self.assertFalse(self.output.exists())

    def test_radius_annotation_preserves_raw_and_requires_exact_unit_conversion(self):
        self.start()
        item = candidate("inner_bend_radius", "R .08", "2.032", "inch", "mm")
        result = self.completed([item])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["candidates"][0]["raw_value"], "R .08")
        self.assertEqual(result["candidates"][0]["value"], "2.032")
        self.assertEqual(result["candidates"][0]["status"], "pending_review")
        original = self.output.read_bytes()
        self.assertEqual(self.bridge.inspect(self.sid)["status"], "completed")
        self.assertEqual(self.output.read_bytes(), original)

    def test_radius_annotation_does_not_relax_other_numeric_or_unit_checks(self):
        self.start()
        for item in (
                candidate("inner_bend_radius", "R .08", "2.03", "inch", "mm"),
                candidate("inner_bend_radius", "R .08", "2.032", "mm", "mm"),
                candidate("inner_bend_radius", "R .08 in", "2.032", "inch", "mm"),
                candidate("inner_bend_radius", "R .08 ± .01", "2.032", "inch", "mm"),
                candidate("inner_bend_radius", "R NaN", "2.032", "inch", "mm"),
                candidate("inner_bend_radius", "R 0", "0", "inch", "mm"),
                candidate("inner_bend_radius", "R .08", "NaN", "inch", "mm"),
                candidate("thickness", "R .08", "2.032", "inch", "mm"),
                candidate("material_usage_per_piece", "R .08", ".08", "kg/件", "kg/piece")):
            with self.subTest(item=item):
                self.assertEqual(self.completed([item])["status"], "failed")
        self.assertFalse(self.output.exists())

    def test_thickness_annotation_preserves_raw_and_observations_with_exact_conversion(self):
        self.start()
        rows = [observation('engineering_open_items', f'NOTE {index}: retain original drawing note',
                            status='observed') for index in range(66)]
        for raw, raw_unit, value in [('T=1.5', 'mm', '1.5'), ('\tt \t= .08\t', 'inch', '2.032')]:
            with self.subTest(raw=raw):
                item = candidate('thickness', raw, value, raw_unit, 'mm')
                text = json.dumps({'candidates': [item], 'observations': rows, 'warnings': []})
                result = self.completed(text=text)
                self.assertEqual(result['status'], 'completed')
                actual = result['candidates'][0]
                self.assertEqual(actual['raw_value'], raw)
                self.assertEqual(actual['raw_unit'], raw_unit)
                self.assertEqual(actual['value'], value)
                self.assertEqual(actual['status'], 'pending_review')
                self.assertEqual([row['raw_text'] for row in result['observations']],
                                 [row['raw_text'] for row in rows])
                original = self.output.read_bytes()
                self.assertEqual(self.bridge.inspect(self.sid)['status'], 'completed')
                self.assertEqual(self.output.read_bytes(), original)
                self.output.unlink()

    def test_thickness_annotation_does_not_relax_other_fields_numeric_units_or_mixed_labels(self):
        self.start()
        malformed = ['T 1.5', 'T=1.5 mm', 'T=1.5 ± .1', 'T=1.5/2', 'T=1.5,2',
                     'T=1.5=1.5', 'T=3/2', 'T=1.5+0', 'T=1.5 NOTE', 'T=NaN',
                     'T=Infinity', 'T=-1.5', 'T=+1.5', 'T=1.5e0', 'T=\n1.5', 'T=1.5\n']
        items = [candidate('thickness', raw, '1.5', 'mm', 'mm') for raw in malformed]
        items.extend([
            candidate('thickness', 'T=0', '0', 'mm', 'mm'),
            candidate('thickness', 't=.08', '2.03', 'in', 'mm'),
            candidate('thickness', 'T=1.5', '1.5', 'cm', 'mm'),
            candidate('thickness', 'T=1.5', 'NaN', 'mm', 'mm'),
            candidate('thickness', 'T=1.5', True, 'mm', 'mm'),
            candidate('inner_bend_radius', 'T=1.5', '1.5', 'mm', 'mm'),
            candidate('material_usage_per_piece', 'T=1.5', '1.5', 'kg/piece', 'kg/piece'),
        ])
        for item in items:
            with self.subTest(item=item):
                self.assertEqual(self.completed([item])['status'], 'failed')
                self.assertFalse(self.output.exists())

    def test_drawing_units_redundant_raw_unit_preserves_67_observations_and_original_response(self):
        self.start()
        prompt = next(payload['content'][0]['text'] for method, payload, _ in self.bridge.calls
                      if method == 'session.prompt')
        self.assertIn('text 字段的 unit 和 raw_unit 都必须为 null', prompt)
        self.assertIn('完整标注另存 observations', prompt)
        self.assertIn('quantity 的 value 必须是正整数 JSON 数字', prompt)
        for drawing_unit in ('mm', 'in', 'inch'):
            with self.subTest(drawing_unit=drawing_unit):
                rows = [observation('engineering_open_items', f'NOTE {index}: original drawing note',
                                    status='observed') for index in range(66)]
                rows.append(observation('drawing_units', f'DIMENSIONS IN {drawing_unit}',
                                        raw_unit=drawing_unit, status='observed'))
                units = candidate('drawing_units', drawing_unit, drawing_unit, drawing_unit, None)
                items = [units, candidate('product_name', 'Bracket', 'Bracket', None, None),
                         candidate('material_family', 'Stainless steel', 'Stainless steel', None, None),
                         candidate('material_grade', 'SS304', 'SS304', None, None),
                         candidate('thickness', 'T=1.5', '1.5', 'mm', 'mm'),
                         candidate('inner_bend_radius', 'R .08', '2.032', 'inch', 'mm'),
                         candidate('surface_treatment', 'Deburr', 'Deburr', None, None)]
                payload = {'candidates': items, 'observations': rows, 'warnings': []}
                before = deepcopy(payload)
                text = json.dumps(payload)
                result = self.completed(text=text)
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(len(result['candidates']), 7)
                actual = result['candidates'][0]
                self.assertEqual((actual['raw_value'], actual['value']), (drawing_unit, drawing_unit))
                self.assertIsNone(actual['raw_unit'])
                self.assertIsNone(actual['unit'])
                self.assertTrue(all(item['status'] == 'pending_review' for item in result['candidates']))
                self.assertEqual(result['observations'], rows)
                self.assertEqual(result['text'], text)
                self.assertEqual(payload, before)
                saved = self.output.read_bytes()
                self.assertEqual(json.loads(saved)['text'], text)
                self.assertEqual(self.bridge.inspect(self.sid), result)
                self.assertEqual(self.output.read_bytes(), saved)
                self.output.unlink()

    def test_drawing_units_raw_unit_normalization_rejects_conflicts_unknown_units_and_other_text(self):
        self.start()
        items = [
            candidate('drawing_units', 'cm', 'cm', 'cm', None),
            candidate('drawing_units', 'MM', 'MM', 'MM', None),
            candidate('drawing_units', 'mm', 'in', 'mm', None),
            candidate('drawing_units', 'mm', 'mm', 'inch', None),
            candidate('drawing_units', 'mm', 'mm', 'mm', 'mm'),
            candidate('drawing_units', 'mm', 'mm', 'mm', 'inch'),
            candidate('drawing_units', 'mm', 'mm', ' mm ', None),
            candidate('product_name', 'mm', 'mm', 'mm', None),
            candidate('material_grade', 'mm', 'mm', 'mm', None),
            candidate('surface_treatment', 'mm', 'mm', 'mm', None),
        ]
        for item in items:
            with self.subTest(item=item):
                self.assertEqual(self.completed([item])['status'], 'failed')
                self.assertFalse(self.output.exists())

    def test_text_process_route_is_deferred_without_fabricating_a_raw_list(self):
        self.start()
        raw = "automated cutting; bending UP 90°"
        route = candidate("process_route", raw, ["自动切割", "向上折弯90°"], None, None)
        material = candidate("material_grade", "A36 STEEL", "A36 STEEL", None, None)
        radius = candidate("inner_bend_radius", "R .08", "2.032", "inch", "mm")
        gauge = candidate("thickness", "16 GAUGE", "16 gauge", "gauge", None)
        result = self.completed([gauge, material, radius, route])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["name"] for item in result["candidates"]], ["material_grade", "inner_bend_radius"])
        self.assertIn(PROCESS_ROUTE_WARNING, result["warnings"])
        self.assertIn(GAUGE_WARNING, result["warnings"])
        original_text = result["text"]
        source_json = json.loads(original_text.removeprefix("```json\n").removesuffix("\n```"))
        self.assertEqual(source_json["candidates"][-1]["raw_value"], raw)
        original = self.output.read_bytes()
        self.assertEqual(json.loads(original)["text"], original_text)
        self.assertEqual(self.bridge.inspect(self.sid)["status"], "completed")
        self.assertEqual(self.output.read_bytes(), original)

    def test_process_route_deferral_keeps_list_shape_units_and_evidence_strict(self):
        self.start()
        route = candidate("process_route", "automated cutting; bending UP 90°", ["自动切割", "向上折弯90°"], None, None)
        malformed = [
            {**route, "raw_value": " "},
            {**route, "raw_value": "x" * 10001},
            {**route, "value": []},
            {**route, "value": [""]},
            {**route, "value": [True]},
            {**route, "raw_unit": "mm"},
            {**route, "unit": "mm"},
            {**route, "raw_value": ["different route"]},
            {**route, "raw_value": {"text": "cutting"}},
            {**route, "name": "fee_inclusions"},
            {**route, "evidence": {**route["evidence"], "reference": "other-task"}},
        ]
        for item in malformed:
            with self.subTest(item=item):
                self.assertEqual(self.completed([item])["status"], "failed")
        self.assertEqual(self.completed([route, route])["status"], "failed")
        self.assertFalse(self.output.exists())

    def test_progress_empty_history_has_no_invented_activity_or_percentage(self):
        self.start()
        first = self.bridge.inspect(self.sid)["progress"]
        self.assertEqual(first["stage"], "queued")
        self.assertIsNone(first["last_event_seq"])
        self.assertIsNone(first["last_activity_at"])
        self.assertEqual(first["events"], [])
        self.assertEqual(first, self.bridge.inspect(self.sid)["progress"])
        self.assertNotIn("percent", first)
        self.assertNotIn("pages_read", first)

    def test_progress_uses_real_epoch_milliseconds_and_scrubs_model_and_tool_contents(self):
        self.start()
        epoch = 1792378800000
        secrets = ["DO NOT DISPLAY THOUGHT", "DO NOT DISPLAY ANSWER", "/srv/private/customer-secret.png"]
        self.bridge.events = [timed_event(1, "turn/start", epoch), timed_event(2, "step/start", epoch + 100, step=1),
                              timed_event(3, "assistant/chunk", epoch + 200, step=1,
                                          chunk={"type": "reasoning-delta", "index": 0, "text": secrets[0]}),
                              timed_event(4, "assistant/chunk", epoch + 300, step=1,
                                          chunk={"type": "text-delta", "index": 1, "text": secrets[1]}),
                              timed_event(5, "tool/call", epoch + 400, step=1, name="read_image", callId="image-1",
                                          arguments=json.dumps({"path": secrets[2]}))]
        progress = self.bridge.inspect(self.sid)["progress"]
        self.assertEqual(progress["stage"], "reading_files")
        self.assertEqual(progress["last_event_seq"], 5)
        self.assertEqual(progress["last_activity_at"], epoch + 400)
        self.assertEqual(progress["events"][-1]["at"], epoch + 400)
        summary = json.dumps(progress, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, summary)
        self.assertNotIn("arguments", summary)
        self.bridge.events.append(timed_event(6, "tool/result", epoch + 500, step=1,
                                             message={"source": {"kind": "tool", "callId": "image-1"},
                                                      "content": [{"type": "tool-result", "toolCallId": "image-1", "content": [{"type": "text", "text": secrets[2]}]}]}))
        self.assertEqual(self.bridge.inspect(self.sid)["progress"]["stage"], "waiting_model")
        self.bridge.events.append(timed_event(7, "assistant/chunk", epoch + 600, step=2,
                                             chunk={"type": "reasoning-delta", "index": 0, "text": secrets[0]}))
        self.assertEqual(self.bridge.inspect(self.sid)["progress"]["stage"], "extracting")

    def test_progress_retry_uses_rc2_retry_and_delay_fields_without_failure_details(self):
        self.start()
        epoch = 1792378800000
        self.bridge.events = [timed_event(1, "turn/start", epoch),
                              timed_event(2, "llm/retry", epoch + 300000, step=1, retry=2, delayMs=462,
                                          provider="secret-provider", policyKey="secret-policy", retryId="retry-private",
                                          failure={"code": "TIMEOUT", "message": "secret-path /private/credentials"})]
        first = self.bridge.inspect(self.sid)["progress"]
        self.assertEqual(first["stage"], "retrying")
        self.assertEqual(first["retry"], {"attempt": 2, "delay_ms": 462})
        self.assertEqual(first, self.bridge.inspect(self.sid)["progress"])
        self.assertNotIn("secret", json.dumps(first))
        self.bridge.events.append(timed_event(3, "llm/retry-started", epoch + 300462, step=1, retry=2, retryId="retry-private"))
        resumed = self.bridge.inspect(self.sid)["progress"]
        self.assertEqual(resumed["stage"], "waiting_model")
        self.assertEqual(resumed["last_activity_at"], epoch + 300462)
        self.assertEqual(resumed["retry"]["attempt"], 2)

    def test_progress_parallel_read_tool_results_do_not_claim_all_files_read(self):
        self.start()
        self.bridge.events = [timed_event(1, "tool/call", 1000, name="read_image", callId="one", arguments="{}"),
                              timed_event(2, "tool/call", 1001, name="read", callId="two", arguments="{}"),
                              timed_event(3, "tool/result", 1002, message={"source": {"kind": "tool", "callId": "one"}, "content": []})]
        progress = self.bridge.inspect(self.sid)["progress"]
        self.assertEqual(progress["stage"], "reading_files")
        self.assertNotIn("全部", progress["label"])
        self.assertNotIn("已读完", progress["label"])

    def test_progress_is_bounded_to_current_turn_without_untrusted_timestamps(self):
        self.start()
        self.bridge.events = [timed_event(1, "llm/retry", 1000, turn=1, retry=99, delayMs=100)]
        self.bridge.events += [timed_event(seq, "step/start" if seq % 2 else "assistant/chunk", 1000 + seq, turn=2,
                                          step=seq, chunk={"type": "reasoning-delta", "text": "private thought"}) for seq in range(2, 42)]
        self.bridge.events[-1]["event"]["time"] = "model-controlled timestamp"
        progress = self.bridge.inspect(self.sid)["progress"]
        self.assertEqual(len(progress["events"]), 12)
        self.assertEqual(progress["last_event_seq"], 41)
        self.assertEqual(progress["last_activity_at"], 1040)
        self.assertNotIn("retry", progress)
        self.assertNotIn("model-controlled", json.dumps(progress))

    def test_progress_unknown_and_failed_never_invent_new_activity(self):
        self.start()
        self.bridge.events = [timed_event(1, "turn/start", 1000)]
        self.bridge.failures["session.list"] = DshBridgeError("network unavailable", "transport-error")
        result = self.bridge.inspect(self.sid)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["progress"]["stage"], "unknown")
        self.assertEqual(result["progress"]["last_activity_at"], 1000)
        self.bridge.failures.clear()
        self.bridge.events.append(timed_event(2, "turn/end", 300000, reason={"kind": "error", "error": {"code": "TIMEOUT", "message": "secret failure body"}}))
        result = self.bridge.inspect(self.sid)
        self.assertEqual(result["progress"]["stage"], "failed")
        self.assertNotIn("secret", json.dumps(result["progress"]))

    def test_completed_result_keeps_legacy_candidate_file_bytes_and_progress_external(self):
        self.start()
        self.legacy_manifest()
        first = self.completed()
        self.assertNotIn("observations", first)
        legacy = {key: value for key, value in first.items() if key != "progress"}
        # Explicitly install the old v1 artifact shape; progress is runtime-only.
        self.output.write_text(json.dumps(legacy, ensure_ascii=False, indent=2) + "\n")
        before = self.output.read_bytes()
        self.bridge.events[-1]["event"]["time"] = 1792378800000
        current = self.bridge.inspect(self.sid)
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["progress"]["stage"], "completed")
        self.assertEqual(current["progress"]["events"][-1]["stage"], "completed")
        self.assertEqual(self.output.read_bytes(), before)
        self.assertNotIn("progress", json.loads(before))

    def test_multi_evidence_preserves_gauge_and_parenthesized_dimension_without_guessing_mm(self):
        self.start()
        items = [observation(raw_text="16 GAUGE A36 STEEL", raw_unit="Gauge"), observation()]
        items[0]["evidence"]["region"] = "左上角材料说明"
        payload = {"observations": items, "candidates": [], "warnings": ["板厚毫米值待核对"]}
        result = self.completed(text=json.dumps(payload))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["observations"], items)
        self.assertEqual(result["candidates"], [])
        self.assertNotIn("1.524", json.dumps(result["observations"]))
        before = self.output.read_bytes()
        self.assertEqual(json.loads(before)["observations"], items)
        self.assertEqual(self.bridge.inspect(self.sid)["observations"], items)
        self.assertEqual(self.output.read_bytes(), before)

    def test_conflicting_and_unreadable_observations_remain_unconfirmed_and_separate_from_candidates(self):
        self.start()
        items = [observation(raw_text="3", raw_unit="mm", status="conflict"),
                 observation(raw_text="(.060)", status="conflict"),
                 observation("material_grade", raw_text="[无法辨认]", status="unreadable")]
        result = self.completed(text=json.dumps({"candidates": [], "observations": items}))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual([row["status"] for row in result["observations"]], ["conflict", "conflict", "unreadable"])

    def test_part_observation_aliases_preserve_each_raw_label_status_and_source_without_making_parameters(self):
        self.start()
        rows = [
            observation("overall_size_part01", "3120 × 1870 × 400", "mm", "observed",
                        evidence={"reference": "drawing-1", "page": 1, "region": "零件01外形尺寸"},
                        reason="多视图中的外形标注"),
            observation("drawing_version_part64", "Rev C / Rev D", None, "conflict",
                        evidence={"reference": "drawing-1", "page": 2, "region": "零件64标题栏"},
                        reason="标题栏版本与备注不一致"),
            observation("feature_part02", "Ø12 × 4", None, "unit_unknown",
                        evidence={"reference": "drawing-1", "page": 1, "region": "零件02孔标注"},
                        reason=None),
            observation("drawing_content_partA-01_x", "[无法辨认]", None, "unreadable",
                        evidence={"reference": "drawing-1", "page": 3, "region": "零件A-01说明"},
                        reason="原图文字模糊"),
        ]
        usable = candidate("product_name", "Multi-part fabrication", "Multi-part fabrication", None, None)
        payload = {"candidates": [usable], "observations": rows, "warnings": ["已有原始警示"]}
        result = self.completed(text=json.dumps(payload, ensure_ascii=False))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0], {**usable, "status": "pending_review"})
        self.assertEqual(len(result["observations"]), len(rows))
        for original, mapped in zip(rows, result["observations"]):
            self.assertEqual(mapped["name"], "engineering_open_items")
            for key in ("raw_text", "raw_unit", "status", "evidence"):
                self.assertEqual(mapped[key], original[key])
            self.assertIn(original["name"], mapped["reason"])
            if original["reason"] is not None:
                self.assertIn(original["reason"], mapped["reason"])
            self.assertNotIn("value", mapped)
        self.assertIn("已有原始警示", result["warnings"])
        self.assertEqual(sum("多零件" in warning for warning in result["warnings"]), 1)
        self.assertEqual(json.loads(result["text"]), payload)
        before = self.output.read_bytes()
        repeated = self.bridge.inspect(self.sid)
        self.assertEqual(repeated["status"], "completed")
        self.assertEqual(repeated["observations"], result["observations"])
        self.assertEqual(repeated["warnings"], result["warnings"])
        self.assertEqual(self.output.read_bytes(), before)

    def test_part_observation_aliases_keep_legacy_mapping_while_other_printable_labels_are_preserved(self):
        self.start()
        boundary = observation("overall_size_part" + "A" * 64, "100 × 50", "mm", "observed")
        result = self.completed(text=json.dumps({"candidates": [], "observations": [boundary]}))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["observations"][0]["name"], "engineering_open_items")
        self.output.unlink()
        for name in ("total_price", "total_price_part01", "quote_part01", "material_grade_part01",
                     "unknown_part01", "overall_size_part", "overall_size_part" + "A" * 65,
                     "feature_part零件01", "drawing_content_part01/02",
                     "prefix_drawing_version_part01", "drawing_versions_part01"):
            with self.subTest(name=name):
                item = observation(name, "原始观察", None, "observed")
                result = self.completed(text=json.dumps({"candidates": [candidate()], "observations": [item]}))
                self.assertEqual(result["status"], "completed", result.get("error"))
                self.assertEqual(result["observations"], [{**item, "name": "engineering_open_items",
                                                         "source_name": name}])
                self.assertEqual(len(result["candidates"]), 1)
                self.output.unlink()
        invalid = observation("feature_part01\n", "原始观察", None, "observed")
        result = self.completed(text=json.dumps({"candidates": [candidate()], "observations": [invalid]}))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.output.exists())

    def test_part_observation_aliases_keep_shape_bounds_state_and_cross_task_checks_strict(self):
        self.start()
        original = observation("feature_part01", "4 × Ø12", "mm", "observed", reason="原标注")
        invalid = []
        for update in ({"raw_text": " "}, {"raw_text": "x" * 10001}, {"raw_unit": 12},
                       {"status": "verified"}, {"status": "pending_review"}, {"status": None},
                       {"reason": 12}, {"reason": "x" * 4001}, {"reason": "x" * 4000},
                       {"value": ["新增候选"]}):
            invalid.append({**original, **update})
        missing_reason = deepcopy(original)
        del missing_reason["reason"]
        invalid.append(missing_reason)
        for update in ({"reference": "other-task"}, {"page": 0}, {"page": True},
                       {"region": ""}, {"region": "x" * 1001}):
            invalid.append({**original, "evidence": {**original["evidence"], **update}})
        for item in invalid:
            with self.subTest(item=str(item)[:120]):
                result = self.completed(text=json.dumps({"candidates": [candidate()], "observations": [item]}))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["candidates"], [])
                self.assertFalse(self.output.exists())

    def test_part_observation_aliases_do_not_become_candidate_aliases_or_escape_review_scope(self):
        self.start()
        aliased_candidate = candidate("overall_size_part01", "100 × 50", "100 × 50", None, None)
        mapped_observation = observation("overall_size_part01", "100 × 50", "mm", "observed")
        result = self.completed(text=json.dumps({"candidates": [aliased_candidate],
                                                 "observations": [mapped_observation]}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertFalse(self.output.exists())
        run = self.bridge._run(self.sid)
        run["review_fields"] = ["thickness"]
        with self.assertRaises(ValueError):
            self.bridge.observations_from_text(json.dumps({"candidates": [],
                                                          "observations": [mapped_observation]}), run)

    def test_observation_contract_rejects_invalid_sources_states_shapes_and_bounds_atomically(self):
        self.start()
        variants = []
        for update in ({"name": ""}, {"name": []}, {"raw_text": " "},
                       {"raw_text": "x" * 10001}, {"raw_unit": 16}, {"raw_unit": "x" * 4001},
                       {"status": "verified"}, {"status": None}, {"value": 1.524},
                       {"reason": "x" * 4001}):
            variants.append(observation(**update))
        missing = observation()
        del missing["reason"]
        variants.append(missing)
        for update in ({"reference": "other-task"}, {"reference": "inquiry"}, {"page": 0},
                       {"page": True}, {"page": 10001}, {"page": "1"}, {"region": ""},
                       {"region": "x" * 1001}, {"reference": []}):
            item = observation()
            item["evidence"].update(update)
            variants.append(item)
        for item in variants:
            with self.subTest(item=str(item)[:150]):
                result = self.completed(text=json.dumps({"candidates": [candidate()], "observations": [item]}))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["candidates"], [])
                self.assertNotIn("observations", result)
                self.assertFalse(self.output.exists())
        for items in (None, {}, [observation()] * (MAX_OBSERVATIONS + 1)):
            with self.subTest(kind=type(items).__name__):
                self.assertEqual(self.completed(text=json.dumps({"candidates": [], "observations": items}))["status"], "failed")
                self.assertFalse(self.output.exists())

    def test_observation_inquiry_requires_actual_text_and_null_page(self):
        self.start()
        item = observation("quantity", raw_text="100件", raw_unit="件", status="observed")
        item["evidence"] = {"reference": "inquiry", "page": None, "region": "询价第一句"}
        result = self.completed(text=json.dumps({"candidates": [], "observations": [item]}))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["observations"], [item])
        run = self.bridge._run(self.sid)
        run["inquiry"] = " "
        with self.assertRaises(ValueError):
            self.bridge.observations_from_text(result["text"], run)

    def test_legacy_evidence_projection_is_pure_and_preserves_deferred_raw_labels(self):
        self.start()
        self.legacy_manifest()
        rows = [candidate(raw="16 GAUGE", value="16", raw_unit="Gauge", unit=None),
                candidate("process_route", raw="BEND UP 90°", value=["折弯"], raw_unit=None, unit=None),
                candidate("product_name", raw="Bracket", value="Bracket", raw_unit=None, unit=None)]
        result = self.completed(rows)
        self.assertNotIn("observations", result)
        before = {path.name: path.read_bytes() for path in self.workspace.iterdir() if path.is_file()}
        calls_before = deepcopy(self.bridge.calls)
        projected = self.bridge.observations_from_text(result["text"], self.bridge._run(self.sid))
        self.assertEqual([row["raw_text"] for row in projected], ["16 GAUGE", "BEND UP 90°", "Bracket"])
        self.assertEqual(projected[0]["status"], "unit_unknown")
        self.assertEqual(projected[0]["reason"], GAUGE_WARNING)
        self.assertEqual(projected[1]["status"], "observed")
        self.assertEqual(self.bridge.calls, calls_before)
        self.assertEqual({path.name: path.read_bytes() for path in self.workspace.iterdir() if path.is_file()}, before)
        self.assertNotIn("observations", json.loads(self.output.read_text()))

    def test_new_run_falls_back_to_valid_raw_evidence_when_model_omits_observations(self):
        self.start()
        self.assertEqual(self.bridge._run(self.sid)["observation_version"], 1)
        rows = [candidate(raw="16 GAUGE", value="16", raw_unit="Gauge", unit=None),
                candidate("material_grade", raw="A36", value="A36", raw_unit=None, unit=None)]
        result = self.completed(rows)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["raw_text"] for item in result["observations"]], ["16 GAUGE", "A36"])
        self.assertEqual(result["observations"][0]["status"], "unit_unknown")
        self.assertEqual([item["name"] for item in result["candidates"]], ["material_grade"])
        before = self.output.read_bytes()
        self.assertEqual(json.loads(before)["observations"], result["observations"])
        self.assertEqual(self.bridge.inspect(self.sid)["observations"], result["observations"])
        self.assertEqual(self.output.read_bytes(), before)

    def test_reusing_legacy_manifest_does_not_upgrade_protocol_or_rewrite_artifact(self):
        self.start()
        self.legacy_manifest()
        first = self.completed()
        before = self.output.read_bytes()
        starts = len(self.bridge.calls)
        again = self.bridge.start("TASK-1", self.files, "请读当前附件", self.output)
        self.assertTrue(again["reused"])
        self.assertEqual(len(self.bridge.calls), starts)
        self.assertNotIn("observation_version", self.bridge._run(self.sid))
        current = self.bridge.inspect(self.sid)
        self.assertNotIn("observations", first)
        self.assertNotIn("observations", current)
        self.assertEqual(self.output.read_bytes(), before)

    def test_legacy_projection_does_not_hide_invalid_candidates_or_invent_extra_observations(self):
        self.start()
        run = self.bridge._run(self.sid)
        for rows in ([candidate(raw_unit="unknown-unit")], [candidate(), candidate()],
                     [candidate(raw="NaN", value="16", raw_unit="Gauge", unit=None)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.bridge.observations_from_text(json.dumps({"candidates": rows}), run)
        payload = {"candidates": [candidate()], "observations": []}
        self.assertEqual(self.bridge.observations_from_text(json.dumps(payload), run), [])
        payload["observations"] = [observation()]
        self.assertEqual(self.bridge.observations_from_text(json.dumps(payload), run), [observation()])

    def test_prompt_requires_per_page_original_evidence_and_distinguishes_reference_units(self):
        self.start()
        prompt = self.bridge.calls[-1][1]["content"][0]["text"]
        for expected in ("逐页检查", "材料注", "标题栏", "侧视图", "括号", "局部放大", "observations",
                         "同字段允许多条", "Gauge", "unit_unknown", "每字段最多一个", "read_image"):
            self.assertIn(expected, prompt)
        self.assertNotIn("review_fields", self.bridge._run(self.sid))

    def test_review_scope_is_persisted_idempotent_and_uses_only_one_prompt(self):
        self.start(review_fields=["thickness", "drawing_units"])
        run = self.bridge._run(self.sid)
        self.assertEqual(run["review_fields"], ["thickness", "drawing_units"])
        prompt = self.bridge.calls[-1][1]["content"][0]["text"]
        self.assertIn("本轮是关键缺项回查", prompt)
        self.assertIn("不要补写其他字段", prompt)
        second = self.bridge.start("TASK-1", self.files, "请读当前附件", self.output,
                                   review_fields=["thickness", "drawing_units"])
        self.assertTrue(second["requires_inspect"])
        self.assertEqual(sum(call[0] == "session.prompt" for call in self.bridge.calls), 1)
        for review in (None, ["thickness"]):
            with self.subTest(review=review), self.assertRaises(ValueError):
                self.bridge.start("TASK-1", self.files, "请读当前附件", self.output, review_fields=review)

    def test_invalid_review_scope_is_rejected_before_dispatch_and_results_stay_in_scope(self):
        for fields in ([], "thickness", ["total_price"], ["thickness", "thickness"], [None], [{}]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.start(review_fields=fields)
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse((self.workspace / MANIFEST).exists())
        self.start(review_fields=["thickness"])
        result = self.completed(text=json.dumps({"candidates": [], "observations": [observation()]}))
        self.assertEqual(result["status"], "completed")
        self.output.unlink()
        for payload in ({"candidates": [], "observations": [observation("material_grade")]},
                        {"candidates": [candidate("quantity", raw=100, value=100, raw_unit="件", unit="piece")]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.completed(text=json.dumps(payload))["status"], "failed")
                self.assertFalse(self.output.exists())

    def test_drawing_parts_keep_explicit_membership_raw_units_and_cached_bytes(self):
        self.start()
        rows = [observation("孔位与孔径", "01 | 4X Ø.25 THRU | 2 boxes/set", "in", "observed"),
                observation("外形尺寸", "02 | 80 × 40 mm | 2件/套", "mm", "observed")]
        payload = {"candidates": [], "observations": rows, "warnings": [], "parts": [drawing_part(), drawing_part("part-02", "侧板", "02", "A", [1])]}
        result = self.completed(text=json.dumps(payload))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["candidates"], [])
        self.assertEqual([part["part_number"] for part in result["parts"]], ["01", "02"])
        self.assertEqual([part["status"] for part in result["parts"]], ["pending_review"] * 2)
        self.assertEqual(result["parts"][0]["observations"], [result["observations"][0]])
        self.assertEqual(result["parts"][1]["observations"][0]["raw_text"], rows[1]["raw_text"])
        self.assertEqual(result["parts"][0]["observations"][0]["raw_unit"], "in")
        before = self.output.read_bytes()
        self.assertEqual(self.bridge.inspect(self.sid), result)
        self.assertEqual(self.output.read_bytes(), before)

    def test_part_evidence_and_observations_map_derived_pages_before_source_validation(self):
        self.start()
        run = self.bridge._run(self.sid)
        run["pdf_views"] = {"page_counts": {"drawing-1": 2}, "views": [
            {"id": "PAGE-2", "origin_id": "drawing-1", "page": 2}]}
        run["detail_views"] = {"views": [
            {"id": "DETAIL-2", "origin_id": "drawing-1", "page": 2, "region": "左上"}]}
        row = observation("孔位与孔径", "4X Ø6", "mm", "observed",
                          evidence={"reference": "DETAIL-2", "page": 1, "region": "孔位图"})
        payload = {"candidates": [], "observations": [row], "parts": [drawing_part(
            evidence=[{"reference": "PAGE-2", "page": 1, "region": "标题栏"}])]}
        parts = self.bridge._parts(payload, run)
        self.assertEqual(parts[0]["evidence"], [{"reference": "drawing-1", "page": 2, "region": "标题栏"}])
        self.assertEqual(parts[0]["observations"][0]["evidence"],
                         {"reference": "drawing-1", "page": 2, "region": "左上；孔位图"})
        self.assertEqual(self.bridge._candidates(json.dumps(payload), run)[0], [])
        payload["parts"][0]["evidence"][0]["page"] = 2
        with self.assertRaises(ValueError):
            self.bridge._parts(payload, run)

    def test_part_views_map_pdf_page_but_reject_detail_coordinates_and_other_page(self):
        self.start()
        run = self.bridge._run(self.sid)
        run["pdf_views"] = {"page_counts": {"drawing-1": 2}, "views": [
            {"id": "PAGE-2", "origin_id": "drawing-1", "page": 2}]}
        run["detail_views"] = {"views": [
            {"id": "DETAIL-2", "origin_id": "drawing-1", "page": 2, "region": "左上"}]}
        item = drawing_part(evidence=[{"reference": "drawing-1", "page": 2, "region": "标题栏"}])
        view = {"label": "轴测图", "reference": "PAGE-2", "page": 1,
                "bbox": [100, 100, 600, 500], "hotspots": []}
        item["views"] = [view, {**view, "reference": "DETAIL-2"},
                         {**view, "reference": "drawing-1", "page": 1},
                         {**view, "reference": "PAGE-2", "page": 2}]
        row = observation(evidence={"reference": "drawing-1", "page": 2, "region": "NOTES"})
        payload = {"candidates": [], "observations": [row], "parts": [item]}
        result = self.bridge._parts(payload, run)
        self.assertEqual(result[0]["views"], [{**view, "reference": "drawing-1", "page": 2}])
        self.assertEqual(result[0]["observations"][0]["raw_text"], row["raw_text"])

    def test_invalid_optional_part_views_complete_without_losing_source_text_or_candidates(self):
        self.start()
        item = drawing_part()
        item["views"] = [{"label": "bad", "reference": "drawing-1", "page": 1,
                          "bbox": [-1, 100, 600, 500], "hotspots": []}]
        payload = {"candidates": [candidate()], "observations": [observation()],
                   "parts": [item], "warnings": []}
        result = self.completed(text=json.dumps(payload))
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["parts"][0]["views"], [])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["observations"][0]["raw_text"], observation()["raw_text"])
        saved = self.output.read_bytes()
        self.assertEqual(self.bridge.inspect(self.sid), result)
        self.assertEqual(self.output.read_bytes(), saved)

    def test_invalid_part_never_returns_partial_success_in_any_bridge_entry(self):
        self.start()
        run = self.bridge._run(self.sid)
        for part in (drawing_part(indices=[True]), drawing_part(indices=[1]), drawing_part(indices=[0, 0]),
                     drawing_part(evidence=[{"reference": "unrelated-task", "page": 1, "region": "标题栏"}]),
                     {**drawing_part(), "status": "verified"}):
            payload = {"candidates": [candidate()], "observations": [observation()], "parts": [part]}
            text = json.dumps(payload)
            with self.subTest(part=part):
                for method in (self.bridge._candidates, self.bridge.observations_from_text):
                    with self.assertRaises(ValueError):
                        method(text, run)
                result = self.completed(text=text)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["candidates"], [])
                self.assertNotIn("observations", result)
                self.assertNotIn("parts", result)
                self.assertFalse(self.output.exists())

    def test_part_quarantines_other_attachment_link_but_rejects_inquiry_evidence(self):
        second = self.workspace / "second.png"
        second.write_bytes(b"second isolated fake image")
        self.files.append({"id": "drawing-2", "path": str(second)})
        self.start()
        row = observation(evidence={"reference": "drawing-2", "page": 1, "region": "图纸"})
        payload = {"candidates": [], "observations": [row], "parts": [drawing_part(
            evidence=[{"reference": "inquiry", "page": None, "region": "询价原文"}])]}
        self.assertEqual(self.completed(text=json.dumps(payload))["status"], "failed")
        self.assertFalse(self.output.exists())
        payload["parts"] = [drawing_part()]
        result = self.completed(text=json.dumps(payload))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["parts"][0]["observations"], [])
        self.assertEqual(result["observations"][0]["raw_text"], row["raw_text"])
        self.assertEqual(result["parts"][0]["evidence"], payload["parts"][0]["evidence"])
        self.assertTrue(any("1 条分件关联" in text for text in result["warnings"]))
        payload["parts"] = [drawing_part(evidence=[{"reference": "drawing-2", "page": 1, "region": "标题栏"}])]
        parts = self.bridge._parts(payload, self.bridge._run(self.sid))
        self.assertEqual(parts[0]["observations"], result["observations"])

    def test_five_parts_keep_78_global_rows_and_quarantine_only_three_cross_part_links(self):
        # Synthetic text only: five drawing sources plus a shared summary.
        for reference in ("drawing-2", "drawing-3", "drawing-4", "drawing-5", "summary"):
            path = self.workspace / (reference + ".png")
            path.write_bytes(("isolated synthetic " + reference).encode())
            self.files.append({"id": reference, "path": str(path)})
        self.start()
        rows = []
        for part_index in range(5):
            for row_index in range(15):
                rows.append(observation("标题栏信息" if row_index == 0 else "尺寸及技术要求",
                                        f"SYNTH-P{part_index + 1}-R{row_index:02d} | 4X Ø6 ±0.1\n原文第二行，原样保留",
                                        "mm", "observed", evidence={"reference": f"drawing-{part_index + 1}",
                                                                       "page": 1, "region": f"标注 {row_index}"}))
        for index in range(3):
            rows.append(observation("汇总表明细行", f"SYNTH-SUMMARY-{index} | 2 boxes/set | 1.5 mm", "mm", "observed",
                                    evidence={"reference": "summary", "page": 1, "region": f"汇总表第 {index + 1} 行"}))
        parts = [drawing_part(f"part-{index + 1}", f"合成零件 {index + 1}", str(index + 1), "A",
                              list(range(index * 15, (index + 1) * 15)) + [75 + index % 3],
                              [{"reference": f"drawing-{index + 1}", "page": 1, "region": "标题栏"},
                               {"reference": "summary", "page": 1, "region": "汇总表"}])
                 for index in range(5)]
        parts[0]["observation_indices"].append(60)
        parts[3]["observation_indices"].append(30)
        parts[4]["observation_indices"].append(45)
        payload = {"candidates": [], "observations": rows, "parts": parts, "warnings": []}
        original = deepcopy(payload)
        text = json.dumps(payload, ensure_ascii=False)
        result = self.completed(text=text)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["observations"]), 78)
        self.assertEqual([row["raw_text"] for row in result["observations"]], [row["raw_text"] for row in rows])
        self.assertEqual([row["evidence"] for row in result["observations"]], [row["evidence"] for row in rows])
        self.assertEqual(len(result["parts"]), 5)
        for index, part in enumerate(result["parts"]):
            self.assertEqual(part["status"], "pending_review")
            self.assertEqual(part["evidence"], parts[index]["evidence"])
            self.assertEqual(part["observations"], result["observations"][index * 15:(index + 1) * 15]
                             + [result["observations"][75 + index % 3]])
        warnings = [warning for warning in result["warnings"] if "条分件关联" in warning]
        self.assertEqual(len(warnings), 1)
        self.assertIn("3 条分件关联", warnings[0])
        self.assertIn("原始标注已完整保留", warnings[0])
        self.assertEqual(payload, original)
        run = self.bridge._run(self.sid)
        self.assertEqual(self.bridge.observations_from_text(text, run), result["observations"])
        candidates, candidate_warnings = self.bridge._candidates(text, run)
        self.assertEqual(candidates, [])
        self.assertEqual([warning for warning in candidate_warnings if "条分件关联" in warning], warnings)
        self.assertTrue(set(candidate_warnings) <= set(result["warnings"]))
        payload["warnings"] = [warnings[0], warnings[0]]
        self.assertEqual(self.bridge._candidates(json.dumps(payload), run)[1].count(warnings[0]), 1)
        saved = self.output.read_bytes()
        self.assertEqual(self.bridge.inspect(self.sid), result)
        self.assertEqual(self.output.read_bytes(), saved)
        self.assertEqual(sum(method == "session.create" for method, *_ in self.bridge.calls), 1)
        self.assertEqual(sum(method == "session.prompt" for method, *_ in self.bridge.calls), 1)
        self.assertEqual((self.workspace / "AGENTS.md").read_text(), READER_INSTRUCTIONS)

    def test_cross_part_link_cannot_bypass_global_observation_validation(self):
        second = self.workspace / "second.png"
        second.write_bytes(b"synthetic second drawing")
        self.files.append({"id": "drawing-2", "path": str(second)})
        self.start()
        run = self.bridge._run(self.sid)
        original = observation(evidence={"reference": "drawing-2", "page": 1, "region": "标题栏"})
        invalid = [{**original, "status": "verified"}, {**original, "extra": "unsupported"},
                   {**original, "reason": "x" * 4001}, {**original, "raw_text": ""},
                   {**original, "evidence": {"reference": "outside-run", "page": 1, "region": "标题栏"}},
                   {**original, "evidence": {"reference": "drawing-2", "page": 0, "region": "标题栏"}},
                   {**original, "evidence": {"reference": "drawing-2", "page": True, "region": "标题栏"}},
                   {**original, "evidence": {"reference": "drawing-2", "page": 1, "region": ""}}]
        for row in invalid:
            payload = {"candidates": [], "observations": [row], "parts": [drawing_part()]}
            text = json.dumps(payload)
            with self.subTest(row=row):
                for method in (self.bridge._candidates, self.bridge.observations_from_text):
                    with self.assertRaises(ValueError):
                        method(text, run)
                self.assertEqual(self.completed(text=text)["status"], "failed")
                self.assertFalse(self.output.exists())

    def test_parts_require_explicit_observations_but_older_replies_remain_unchanged(self):
        self.start()
        payload = {"candidates": [], "parts": []}
        self.assertEqual(self.completed(text=json.dumps(payload))["status"], "failed")
        result = self.completed([])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("parts", result)
        original = self.output.read_bytes()
        self.assertNotIn("parts", json.loads(original))
        self.bridge.inspect(self.sid)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual((self.workspace / "AGENTS.md").read_text(), READER_INSTRUCTIONS)

    def test_parts_full_pass_prompt_and_full_review_but_not_targeted_review(self):
        self.start()
        prompt = self.bridge.calls[-1][1]["content"][0]["text"]
        for required in ("分件识别", "零起整数索引", "首条为0", "不用1起索引", "同号不同版本", "不按附件数",
                         "BOM单套用量", "不凭外观补", '"parts":[]'):
            self.assertIn(required, prompt)
        full = self.bridge._prompt("TASK-1", self.files, "", list(RECOGNITION_FIELDS))
        self.assertIn("分件识别", full)
        self.assertIn('"parts":[]', full)
        narrow = self.bridge._prompt("TASK-1", self.files, "", ["thickness"])
        self.assertIn("本轮定向回查不输出 parts", narrow)
        self.assertNotIn('"parts":[]', narrow)
        run = self.bridge._run(self.sid)
        run["review_fields"] = ["thickness"]
        with self.assertRaises(ValueError):
            self.bridge._candidates(json.dumps({"candidates": [], "observations": [], "parts": []}), run)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = DshBridge("http://127.0.0.1:3080", self.temp.name)

    def response(self, content):
        response = Mock()
        response.read.return_value = content
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.bridge.opener = Mock()
        self.bridge.opener.open.return_value = response

    def test_exact_rc2_request_envelope_and_result(self):
        self.response(b'{"type":"server-response","rpcId":"test-1","result":{"ok":true,"value":{"accepted":true}}}')
        self.assertEqual(self.bridge._rpc("session.cancel", {"sessionId": "s"}, rpc_id="test-1"), {"accepted": True})
        request = self.bridge.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3080/api/session.cancel")
        self.assertEqual(json.loads(request.data), {"type": "client-request", "rpcId": "test-1", "method": "session.cancel", "payload": {"sessionId": "s"}})

    def test_business_rpc_error_not_mistaken_for_http_success(self):
        self.response(b'{"type":"server-response","rpcId":"test-1","result":{"ok":false,"error":{"code":"session-not-found","message":"not found"}}}')
        with self.assertRaises(DshBridgeError) as caught:
            self.bridge._rpc("session.history", {}, rpc_id="test-1")
        self.assertEqual(caught.exception.code, "session-not-found")

    def test_correlation_bad_json_and_truncated_connection(self):
        for body in (b'{"type":"server-response","rpcId":"other","result":{"ok":true,"value":{}}}', b'not json'):
            with self.subTest(body=body):
                self.response(body)
                with self.assertRaises(DshBridgeError) as caught:
                    self.bridge._rpc("session.history", {}, rpc_id="test-1")
                self.assertEqual(caught.exception.code, "protocol-error")
        self.bridge.opener.open.side_effect = IncompleteRead(b"partial")
        with self.assertRaises(DshBridgeError) as caught:
            self.bridge._rpc("session.prompt", {}, rpc_id="test-1")
        self.assertEqual(caught.exception.code, "transport-error")

    def test_local_http_only_and_positive_finite_timeout(self):
        for url in ("https://127.0.0.1:3080", "http://example.com", "http://user:secret@localhost", "http://localhost/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                DshBridge(url, self.temp.name)
        for timeout in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                DshBridge("http://localhost:3080", self.temp.name, timeout)


if __name__ == "__main__":
    unittest.main()
