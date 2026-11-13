"""Source-row quantity semantics; no model, network or customer input needed."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reading_bom import parse_bom_row, project_bom


def observation(text="16 | ECFO8 | Frame earthing M8 | 4", *, region="右上 BOM 第16行", status="observed", source_pass="initial"):
    return {"name": "engineering_open_items", "source_name": "BOM明细行", "raw_text": text,
            "raw_unit": None, "status": status, "reason": None, "source_pass": source_pass,
            "evidence": {"reference": "FILE-01", "page": 1, "region": region}}


def part(rows):
    return {"observations": rows, "evidence": [{"reference": "FILE-01", "page": 1, "region": "BOM第16行"}]}


class ReadingBomTests(unittest.TestCase):
    def test_explicit_four_cells_keep_source_row_scope_and_original_bytes(self):
        original = observation()
        before = deepcopy(original)
        row = parse_bom_row(original)
        self.assertEqual((row["item_no"], row["part_number"], row["description"], row["quantity"]),
                         (16, "ECFO8", "Frame earthing M8", 4))
        self.assertEqual(row["quantity_scope"], "source_bom_row")
        self.assertEqual(row["status"], "pending_review")
        self.assertEqual(row["raw_text"], original["raw_text"])
        row["evidence"]["page"] = 2
        self.assertEqual(original, before)

    def test_chinese_english_and_optional_outer_pipes_have_explicit_row_anchors(self):
        for region in ("右上角BOM第16行", "BOM row 16", "Bill of Materials row 16", "物料清单第16行"):
            with self.subTest(region=region):
                self.assertEqual(parse_bom_row(observation("| 16 | ECF08 | Frame earthing M8 | 4 |", region=region))["quantity"], 4)

    def test_non_bom_numbers_wrong_rows_and_ambiguous_row_provenance_are_not_used(self):
        for region in ("BENDING TABLE第16行", "右上表格第16行", "BOM第15行", "BOM第16行和BOM第17行", "XBOMap第16行"):
            with self.subTest(region=region):
                self.assertIsNone(parse_bom_row(observation(region=region)))
        for source in ({"reference": "inquiry", "page": 1, "region": "BOM第16行"},
                       {"reference": "FILE-01", "page": True, "region": "BOM第16行"},
                       {"reference": "FILE-01", "page": 0, "region": "BOM第16行"}):
            self.assertIsNone(parse_bom_row({**observation(), "evidence": source}))

    def test_requires_readable_single_row_integer_quantity_and_four_cells(self):
        for value in ("0", "-1", "4.5", "4e2", "4 件", "unknown", "[无法辨认]", "9999999999"):
            with self.subTest(value=value):
                self.assertIsNone(parse_bom_row(observation("16 | ECF08 | Frame earthing M8 | " + value)))
        for text in ("16 | ECF08 | 4", "16 | ECF08 | Frame | M8 | 4", "16 | | Frame | 4",
                     "16 | ECF08 | Frame | 4\n17 | ECFE1630 | Rod | 4"):
            self.assertIsNone(parse_bom_row(observation(text)))
        for status in ("conflict", "unreadable", "verified", [], None):
            self.assertIsNone(parse_bom_row(observation(status=status)))

    def test_dimension_unit_unknown_does_not_erase_explicit_qty_cell(self):
        row = observation("16 | ECFE1630 | Rod d=8,5 L=300 | 4", status="unit_unknown")
        self.assertEqual(parse_bom_row(row)["quantity"], 4)
        self.assertEqual(parse_bom_row(row)["quantity_scope"], "source_bom_row")

    def test_part_aggregates_variants_without_selecting_part_number_or_order_qty(self):
        first = observation()
        second = observation("16 | ECF08 | Frame earthing M8 | 4", region="右上角BOM第16行", source_pass="review")
        original = part([first, second])
        before = deepcopy(original)
        value = project_bom(original)
        self.assertEqual(value["part_numbers"], ["ECFO8", "ECF08"])
        self.assertIsNone(value["part_number"])
        self.assertEqual(value["status"], "conflict")
        self.assertEqual(value["identity_status"], "conflict")
        self.assertEqual(value["quantity_status"], "pending_review")
        self.assertEqual(value["quantity"], 4)
        self.assertEqual(value["quantity_scope"], "source_bom_row")
        self.assertEqual(len(value["rows"]), 2)
        self.assertNotIn("order_quantity", value)
        self.assertNotIn("manufacturing_classification", value)
        self.assertEqual(original, before)

    def test_conflicting_qty_is_not_automatically_selected(self):
        value = project_bom(part([observation(), observation("16 | ECFO8 | Frame earthing M8 | 8", source_pass="review")]))
        self.assertIsNone(value["quantity"])
        self.assertEqual(value["quantities"], [4, 8])
        self.assertEqual(value["quantity_status"], "conflict")

    def test_unparsed_or_flagged_same_row_cannot_silently_lose_to_a_valid_pass(self):
        for invalid in (observation(status="conflict", source_pass="review"),
                        observation("16 | ECF08 | Frame | ?", source_pass="review")):
            with self.subTest(row=invalid):
                value = project_bom(part([observation(), invalid]))
                self.assertIsNone(value["quantity"])
                self.assertIsNone(value["part_number"])
                self.assertEqual(value["quantity_status"], "conflict")
                self.assertEqual(value["unresolved_rows"], [invalid])

    def test_multiline_assembly_or_wrong_source_cannot_become_one_bom_item(self):
        other = observation("17 | ECFE1630 | Rod | 4", region="BOM第17行")
        self.assertIsNone(project_bom(part([observation(), other])))
        unlinked = observation()
        unlinked["evidence"]["reference"] = "FILE-02"
        self.assertIsNone(project_bom(part([unlinked])))
        self.assertIsNone(project_bom(part([])))


if __name__ == "__main__":
    unittest.main()
