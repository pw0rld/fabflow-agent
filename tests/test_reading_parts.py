"""Offline tests of evidence-backed part grouping; no model or network."""
from copy import deepcopy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reading_parts import (MAX_PART_LINKS, MAX_PART_OBSERVATIONS, merge_parts, reconcile_parts,
                           validate_model_parts, validate_saved_views)
from reading_bom import project_bom


def evidence(reference="FILE-01", page=1, region="标题栏"):
    return {"reference": reference, "page": page, "region": region}


def observed(text="1.5 mm plate", *, reference="FILE-01", page=1, region="NOTES 1", label="材料原文"):
    return {"name": "engineering_open_items", "source_name": label, "raw_text": text,
            "raw_unit": "mm", "status": "observed", "evidence": evidence(reference, page, region), "reason": None}


def model_part(key="part-01", name="机箱", number="01", version="04", indices=None, sources=None):
    return {"key": key, "name": name, "part_number": number, "version": version,
            "observation_indices": [0] if indices is None else indices,
            "evidence": [evidence()] if sources is None else sources}


def checked(part=None, rows=None):
    return validate_model_parts([part or model_part()], rows or [observed()], deepcopy)


def view(reference="FILE-01", page=1, label="轴测图", bbox=None, hotspots=None):
    return {"label": label, "reference": reference, "page": page,
            "bbox": [100, 100, 600, 500] if bbox is None else bbox,
            "hotspots": [{"label": "盖板", "polygon": [[200, 200], [500, 200], [500, 300], [200, 300]]}]
            if hotspots is None else hotspots}


class ReadingPartsTests(unittest.TestCase):
    def test_optional_views_copy_only_exact_declared_part_source_pages(self):
        original = model_part()
        original["views"] = [view(), view("FILE-02"), view(page=2)]
        before = deepcopy(original)
        result = checked(original)[0]
        self.assertEqual(result["views"], [view()])
        self.assertEqual(original, before)
        result["views"][0]["bbox"][0] = 1
        self.assertEqual(original["views"][0]["bbox"][0], 100)
        self.assertNotIn("views", checked()[0], "legacy parts must keep their old structure")

    def test_invalid_optional_views_never_discard_part_identity_or_original_text(self):
        invalid = [None, {}, [None], [view(bbox=[True, 100, 300, 400])],
                   [view(bbox=[800, 0, 300, 400])], [view(page=True)],
                   [view(hotspots=[{"label": "other part", "polygon": [[0, 0], [10, 0], [10, 10]]}])],
                   [dict(view(), url="https://example.invalid/model.svg")], [view()] * 13]
        for value in invalid:
            part = dict(model_part(), views=value)
            with self.subTest(views=value):
                result = checked(part)[0]
                self.assertEqual(result["views"], [])
                self.assertEqual(result["observations"], [observed()])
                self.assertEqual(result["status"], "pending_review")
        self.assertEqual(validate_saved_views([None, view(), {"url": "x"}], {("FILE-01", 1)}), [view()])

    def test_views_map_full_page_alias_before_membership_and_keep_coordinates(self):
        part = dict(model_part(), views=[view("PDFPAGE-01")])
        def position(reference, page):
            self.assertEqual((reference, page), ("PDFPAGE-01", 1))
            return "FILE-01", 1
        result = validate_model_parts([part], [observed()], deepcopy, view_mapper=position)
        self.assertEqual(result[0]["views"], [view()])

    def test_two_pass_views_merge_regions_on_same_page_without_inventing_geometry(self):
        first = checked(dict(model_part(), views=[view()]))
        second_view = view(hotspots=[{"label": "盒体", "polygon": [[200, 300], [500, 300], [500, 500]]}])
        different_region = view(label="侧视图", bbox=[0, 700, 300, 200], hotspots=[])
        second = checked(dict(model_part("review-01"), views=[second_view, different_region]))
        before = deepcopy((first, second))
        merged = merge_parts(first, second)[0]
        self.assertEqual(len(merged["views"]), 2)
        self.assertEqual([hotspot["label"] for hotspot in merged["views"][0]["hotspots"]], ["盖板", "盒体"])
        self.assertEqual(merged["views"][1], different_region)
        self.assertEqual((first, second), before)

    def test_six_slightly_shifted_view_pairs_keep_one_complete_crop_each(self):
        labels = ["TOP", "侧视图", "开口正面视图", "左下视图", "展开图", "ISOMETRIC"]
        boxes = [[88, 86, 251, 272], [469, 86, 79, 272], [89, 446, 250, 111],
                 [88, 654, 250, 264], [400, 423, 260, 374], [627, 104, 306, 315]]
        next_labels = ["TOP", "上中侧视图", "左中侧面视图", "左下视图", "展开图", "ISOMETRIC"]
        next_boxes = [[87, 85, 254, 273], [468, 86, 80, 273], [88, 447, 252, 111],
                      [88, 653, 253, 270], [400, 421, 259, 340], [628, 107, 304, 306]]
        first = checked(dict(model_part(), views=[view(label=label, bbox=box, hotspots=[]) for label, box in zip(labels, boxes)]))
        second = checked(dict(model_part("review-01"), views=[view(label=label, bbox=box, hotspots=[]) for label, box in zip(next_labels, next_boxes)]))
        before = deepcopy((first, second))
        result = merge_parts(first, second)[0]
        self.assertEqual(len(result["views"]), 6)
        self.assertEqual(result["views"][4]["bbox"], [400, 421, 260, 376])
        self.assertEqual((first, second), before)

    def test_same_label_different_regions_or_incompatible_view_families_remain_distinct(self):
        first = checked(dict(model_part(), views=[view(bbox=[100, 100, 200, 200], hotspots=[])]))
        alternatives = [view(bbox=[301, 100, 200, 200], hotspots=[]),
                        view(bbox=[160, 100, 200, 200], hotspots=[]),
                        view(label="展开图", bbox=[100, 100, 200, 200], hotspots=[]),
                        view(label="SECTION A-A", bbox=[100, 100, 200, 200], hotspots=[])]
        for alternative in alternatives:
            with self.subTest(view=alternative):
                second = checked(dict(model_part("review-01"), views=[alternative]))
                self.assertEqual(len(merge_parts(first, second)[0]["views"]), 2)

    def test_saved_view_reconciliation_preserves_pass_labels_and_original_snapshot(self):
        first = checked(dict(model_part(), views=[view(bbox=[100, 100, 200, 200], hotspots=[]),
                                                 view(bbox=[101, 101, 200, 200], hotspots=[])]))[0]
        first["observations"][0].update(source_pass="review", label="材料原文")
        before = deepcopy(first)
        result = reconcile_parts([first])[0]
        self.assertEqual(len(result["views"]), 1)
        self.assertEqual(result["views"][0]["bbox"], [100, 100, 201, 201])
        self.assertEqual(result["observations"][0]["source_pass"], "review")
        self.assertEqual(result["observations"][0]["label"], "材料原文")
        self.assertEqual(first, before)

    def test_groups_copy_full_raw_observations_without_inventing_parameters(self):
        rows = [observed("01 | 2 boxes/set | 1.5 mm"), observed("02 | 2X Ø.25 THRU", reference="FILE-02", label="孔位与孔径")]
        parts = [model_part(), model_part("part-02", "侧板", "02", "B", [1], [evidence("FILE-02")])]
        result = validate_model_parts(parts, rows, deepcopy)
        self.assertEqual([part["status"] for part in result], ["pending_review"] * 2)
        self.assertEqual(result[0]["observations"], [rows[0]])
        self.assertEqual(result[1]["observations"], [rows[1]])
        self.assertNotIn("quantity", result[0])
        self.assertNotIn("candidates", result[1])
        result[0]["observations"][0]["raw_text"] = "changed"
        self.assertEqual(rows[0]["raw_text"], "01 | 2 boxes/set | 1.5 mm")

    def test_mapping_precedes_source_membership_validation(self):
        def mapped(row):
            self.assertEqual(row["reference"], "DETAIL-01")
            return evidence("FILE-01", 2, "左上；标题栏")
        result = validate_model_parts([model_part(sources=[evidence("DETAIL-01")])],
                                      [observed(page=2)], mapped)
        self.assertEqual(result[0]["evidence"], [evidence("FILE-01", 2, "左上；标题栏")])
        self.assertEqual(result[0]["observations"][0]["evidence"]["page"], 2)

    def test_all_linked_source_references_must_be_covered(self):
        rows = [observed(), observed(reference="FILE-02")]
        with self.assertRaises(ValueError):
            checked(model_part(indices=[0, 1]), rows)
        result = checked(model_part(indices=[0, 1], sources=[evidence(), evidence("FILE-02")]), rows)
        self.assertEqual(len(result[0]["observations"]), 2)

    def test_opt_in_quarantines_only_uncovered_links_without_expanding_sources(self):
        rows = [observed(), observed(reference="FILE-02"),
                observed("共享汇总表原文\n不得改写", reference="SUMMARY")]
        part = model_part(indices=[0, 1, 2], sources=[evidence(), evidence("SUMMARY")])
        before = deepcopy((rows, part))
        skipped = []
        result = validate_model_parts([part], rows, deepcopy, skipped=skipped)
        self.assertEqual(result[0]["observations"], [rows[0], rows[2]])
        self.assertEqual(result[0]["evidence"], part["evidence"])
        self.assertEqual(result[0]["status"], "pending_review")
        self.assertEqual(skipped, [{"part_key": "part-01", "observation_index": 1, "reference": "FILE-02"}])
        self.assertEqual((rows, part), before)
        with self.assertRaises(ValueError):
            validate_model_parts([part], rows, deepcopy)

    def test_uncovered_link_cannot_hide_invalid_observation_fields_or_location(self):
        original = observed(reference="FILE-02")
        changes = [{"status": "verified"}, {"status": True}, {"extra": "unsupported"},
                   {"raw_text": ""}, {"raw_text": "x" * 10001}, {"raw_unit": 1},
                   {"reason": "x" * 4001}, {"source_name": " label"},
                   {"source_pass": "verified"}, {"evidence": evidence("FILE-02", page=0)},
                   {"evidence": evidence("FILE-02", page=True)},
                   {"evidence": evidence("FILE-02", region="")},
                   {"evidence": evidence("inquiry", page=None)}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_model_parts([model_part()], [{**original, **change}], deepcopy, skipped=[])
        for indices in ([True], [-1], [1], [0, 0]):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                validate_model_parts([model_part(indices=indices)], [original], deepcopy, skipped=[])

    def test_opt_in_quarantine_keeps_empty_part_unconfirmed_and_does_not_guess(self):
        rows = [observed(reference="FILE-02")]
        skipped = []
        result = validate_model_parts([model_part()], rows, deepcopy, skipped=skipped)
        self.assertEqual(result[0]["observations"], [])
        self.assertEqual(result[0]["evidence"], [evidence()])
        self.assertEqual(result[0]["status"], "pending_review")
        self.assertEqual(len(skipped), 1)

    def test_only_unique_zero_based_integer_observation_indices_allowed(self):
        for indices in ([True], [False], [-1], [1], [0, 0], [0.0], ["0"], None, {}):
            part = model_part()
            part["observation_indices"] = indices
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                checked(part)

    def test_required_metadata_keys_and_evidence_are_bounded(self):
        for change in ({"key": "not a key"}, {"name": ""}, {"name": " x"}, {"part_number": 2},
                       {"version": ""}, {"version": False}, {"evidence": []}, {"evidence": None},
                       {"name": "x" * 257}, {"cost": 100}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                checked({**model_part(), **change})
        delkey = model_part()
        del delkey["version"]
        with self.assertRaises(ValueError):
            checked(delkey)
        self.assertEqual(validate_model_parts([], [], deepcopy), [])
        with self.assertRaises(ValueError):
            validate_model_parts([model_part()] * 101, [observed()], deepcopy)
        with self.assertRaises(ValueError):
            validate_model_parts([model_part(), model_part()], [observed()], deepcopy)

    def test_inquiry_and_nonmapped_locations_are_not_drawing_parts(self):
        for source in (evidence("inquiry", None), evidence(page=True), evidence(page=0), evidence(region="")):
            with self.subTest(source=source), self.assertRaises(ValueError):
                checked(model_part(sources=[source]))

    def test_per_part_and_whole_reply_link_budgets_reject_without_truncation(self):
        rows = [observed(str(i)) for i in range(MAX_PART_OBSERVATIONS + 1)]
        with self.assertRaises(ValueError):
            checked(model_part(indices=list(range(len(rows)))), rows)
        rows.pop()
        parts = [model_part("part-" + str(i), indices=list(range(len(rows)))) for i in range(5)]
        self.assertEqual(MAX_PART_LINKS, 4 * len(rows))
        with self.assertRaises(ValueError):
            validate_model_parts(parts, rows, deepcopy)
        self.assertEqual(len(validate_model_parts(parts[:4], rows, deepcopy)), 4)

    def test_same_number_version_and_shared_attachment_merge_both_passes(self):
        initial = checked()
        reviewed = checked(model_part("review-01", name="焊接机箱"), [observed("4X Ø6 THRU", label="孔位与孔径")])
        before = deepcopy((initial, reviewed))
        result = merge_parts(initial, reviewed)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "机箱")
        self.assertEqual([row["source_pass"] for row in result[0]["observations"]], ["initial", "review"])
        self.assertEqual((initial, reviewed), before)

    def test_explicit_same_bom_row_joins_ocr_number_variants_but_preserves_both_readings(self):
        initial = checked(model_part(number="ECFO8", version=None),
                          [observed("16 | ECFO8 | Frame earthing M8 | 4", region="右上 BOM 第16行", label="BOM明细行")])
        review = checked(model_part("review-16", number="ECF08", version=None),
                         [observed("16 | ECF08 | Frame earthing M8 | 4", region="右上角BOM第16行", label="BOM明细行")])
        before = deepcopy((initial, review))
        merged = merge_parts(initial, review)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["part_number"], "ECFO8")
        self.assertEqual([row["source_pass"] for row in merged[0]["observations"]], ["initial", "review"])
        self.assertEqual([row["raw_text"] for row in merged[0]["observations"]],
                         [initial[0]["observations"][0]["raw_text"], review[0]["observations"][0]["raw_text"]])
        self.assertEqual(project_bom(merged[0])["part_numbers"], ["ECFO8", "ECF08"])
        self.assertEqual(project_bom(merged[0])["identity_status"], "conflict")
        self.assertEqual((initial, review), before)
        initial[0]["observations"][0].update(source_pass="initial", label="BOM明细行")
        review[0]["observations"][0].update(source_pass="review", label="BOM明细行")
        saved = deepcopy(initial + review)
        resolved = reconcile_parts(saved)
        self.assertEqual(len(resolved), 1)
        self.assertEqual([row["source_pass"] for row in resolved[0]["observations"]], ["initial", "review"])
        self.assertEqual(saved, initial + review)

    def test_bom_row_alias_never_merges_by_name_or_zero_letter_substitution_alone(self):
        first = checked(model_part(number="ECFO8", version=None),
                        [observed("16 | ECFO8 | Frame earthing M8 | 4", region="BOM第16行", label="BOM明细行")])
        variants = [
            (model_part("review-16", number="ECF08", version=None),
             observed("16 | ECF08 | Frame earthing M8 | 4", region="正视图", label="BOM明细行")),
            (model_part("review-16", number="ECF08", version=None),
             observed("17 | ECF08 | Frame earthing M8 | 4", region="BOM第17行", label="BOM明细行")),
            (model_part("review-16", number="ECF08", version="B"),
             observed("16 | ECF08 | Frame earthing M8 | 4", region="BOM第16行", label="BOM明细行")),
            (model_part("review-16", number="ECF08", version=None, sources=[evidence("FILE-02")]),
             observed("16 | ECF08 | Frame earthing M8 | 4", reference="FILE-02", region="BOM第16行", label="BOM明细行")),
        ]
        for part, row in variants:
            with self.subTest(part=part, row=row):
                self.assertEqual(len(merge_parts(first, checked(part, [row]))), 2)
        same_pass = checked(model_part("another-16", number="ECF08", version=None),
                            [observed("16 | ECF08 | Frame earthing M8 | 4", region="BOM第16行", label="BOM明细行")])
        self.assertEqual(len(merge_parts(first + same_pass)), 2)

    def test_equal_part_numbers_on_distinct_bom_rows_are_not_collapsed(self):
        first = checked(model_part(number="FASTENER", version=None),
                        [observed("16 | FASTENER | Fastener | 4", region="BOM第16行", label="BOM明细行")])
        second = checked(model_part("review-17", number="FASTENER", version=None),
                         [observed("17 | FASTENER | Fastener | 8", region="BOM第17行", label="BOM明细行")])
        self.assertEqual(len(merge_parts(first, second)), 2)

    def test_duplicate_observation_dedup_keeps_different_pages_regions_and_categories(self):
        first = checked()
        rows = [observed(), observed(page=2), observed(region="侧视图"), observed(label="厚度原文")]
        second = checked(model_part("review-01", indices=[0, 1, 2, 3]), rows)
        result = merge_parts(first, second)
        self.assertEqual(len(result[0]["observations"]), 4)
        self.assertEqual(result[0]["observations"][0]["source_pass"], "initial")
        self.assertEqual([row["source_pass"] for row in result[0]["observations"]][1:], ["review"] * 3)

    def test_different_versions_numbers_or_disjoint_sources_never_merge(self):
        first = checked()
        for part, rows in ((model_part(version="A"), [observed()]),
                           (model_part(number="02"), [observed()]),
                           (model_part(sources=[evidence("FILE-02")]), [observed(reference="FILE-02")])):
            with self.subTest(part=part):
                result = merge_parts(first, checked(part, rows))
                self.assertEqual(len(result), 2)
                self.assertEqual(len({row["key"] for row in result}), 2)

    def test_only_narrow_version_label_format_is_normalized_for_identity(self):
        first = checked(model_part(version="B"))
        for version in ("Rev B", "REVISION: B", "REV.B", "b"):
            with self.subTest(version=version):
                result = merge_parts(first, checked(model_part(version=version)))
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["version"], "B")
        for first_version, next_version in (("pre-rev03", "rev03"), ("A", "04"), ("04", "4")):
            with self.subTest(versions=(first_version, next_version)):
                self.assertEqual(len(merge_parts(checked(model_part(version=first_version)),
                                                 checked(model_part(version=next_version)))), 2)

    def test_matching_revision_number_alias_merges_five_pairs_without_rewriting_evidence(self):
        initial, reviewed = [], []
        suffixes = ("-REV04", "_revision_04", " REV 01", "REVISION01", "rev03")
        versions = ("04", "04", "01", "01", "03")
        for index, (suffix, version) in enumerate(zip(suffixes, versions), 1):
            reference, number, name = f"SYNTH-{index}", f"P{index}Alloy", f"合成零件 {index}"
            first_row = observed(f"SYNTH-{index} | 初读尺寸 Ø6 ±0.1", reference=reference, region="正视图")
            review_row = observed(f"SYNTH-{index} | 复读技术备注\n原文第二行", reference=reference, region="侧视图")
            initial += checked(model_part(f"part-{index}", name, number, version,
                                          sources=[evidence(reference, region="标题栏")]), [first_row])
            reviewed += checked(model_part(f"review-{index}", name, number + suffix, version,
                                           sources=[evidence(reference, region="侧视图")]), [review_row])
        before = deepcopy((initial, reviewed))
        merged = merge_parts(initial, reviewed)
        self.assertEqual(len(merged), 5)
        for index, part in enumerate(merged):
            self.assertEqual(part["part_number"], initial[index]["part_number"])
            self.assertEqual(part["version"], initial[index]["version"])
            self.assertEqual(part["status"], "pending_review")
            self.assertEqual([row["raw_text"] for row in part["observations"]],
                             [initial[index]["observations"][0]["raw_text"], reviewed[index]["observations"][0]["raw_text"]])
            self.assertEqual([row["source_pass"] for row in part["observations"]], ["initial", "review"])
            self.assertEqual([row["evidence"] for row in part["observations"]],
                             [initial[index]["observations"][0]["evidence"], reviewed[index]["observations"][0]["evidence"]])
            self.assertEqual(part["evidence"], initial[index]["evidence"] + reviewed[index]["evidence"])
        self.assertEqual((initial, reviewed), before)
        # The first pass remains the displayed identity whichever alias it used.
        reverse = merge_parts(reviewed, initial)
        self.assertEqual([part["part_number"] for part in reverse], [part["part_number"] for part in reviewed])

    def test_revision_number_alias_requires_own_version_same_name_and_exact_source_set(self):
        first = checked(model_part(number="P1Alloy", version="03"))
        invalid = [model_part(number="P1Alloy-REV04", version="03"),
                   model_part(number="P1Alloy-REV03", version="04"),
                   model_part(number="P1Alloy-REV03", version=None),
                   model_part(number="P1Alloy-REV03", version="pre-rev03"),
                   model_part(number="P1Alloy03", version="03"),
                   model_part(number="P1Alloy-03", version="03"),
                   model_part(number="P1Alloy-REV3", version="03"),
                   model_part(number="P1Alloy-REV03-extra", version="03"),
                   model_part(number="P1Alloy-REV03", version="03", name="另一零件"),
                   model_part(number="P1Alloy-REV03", version="03", sources=[evidence(), evidence("SUMMARY")])]
        for part in invalid:
            with self.subTest(part=part):
                self.assertEqual(len(merge_parts(first, checked(part))), 2)
        no_version = checked(model_part(number="P1Alloy", version=None))
        self.assertEqual(len(merge_parts(no_version, checked(model_part(number="P1Alloy-REV03", version=None)))), 2)
        # Sharing only the summary still leaves distinct drawings distinct.
        first = checked(model_part(number="P1Alloy", version="03", sources=[evidence(), evidence("SUMMARY")]))
        second = checked(model_part(number="P1Alloy-REV03", version="03",
                                    sources=[evidence("FILE-02"), evidence("SUMMARY")]),
                         [observed(reference="FILE-02")])
        self.assertEqual(len(merge_parts(first, second)), 2)

    def test_exact_number_matches_keep_existing_name_and_shared_source_semantics(self):
        first = checked(model_part(number="P1Alloy", version="03"))
        second = checked(model_part(number="P1Alloy", version="03", name="复读名称",
                                    sources=[evidence(), evidence("SUMMARY")]))
        self.assertEqual(len(merge_parts(first, second)), 1)

    def test_unnumbered_requires_exact_name_version_and_shared_source(self):
        first = checked(model_part(number=None, name="左侧板"))
        self.assertEqual(len(merge_parts(first, checked(model_part(number=None, name="左侧板")))), 1)
        self.assertEqual(len(merge_parts(first, checked(model_part(number=None, name="右侧板")))), 2)
        self.assertEqual(len(merge_parts(first, checked(model_part(number="01", name="左侧板")))), 2)

    def test_normalized_merge_rejects_bad_shapes_states_or_source_membership(self):
        for change in ({"status": "verified"}, {"observations": [{}]}, {"evidence": []}, {"extra": 1}):
            invalid = checked()[0]
            invalid.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                merge_parts([invalid])
        with self.assertRaises(ValueError):
            merge_parts(checked(), {})
        invalid = checked()[0]
        invalid["observations"][0]["evidence"]["reference"] = "other-task"
        with self.assertRaises(ValueError):
            merge_parts([invalid])


if __name__ == "__main__":
    unittest.main()
