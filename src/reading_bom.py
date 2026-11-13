"""Read-only projections of explicit, source-located BOM rows.

A BOM row quantity is never an order quantity. No dimensions, procurement
classification or missing table cells are inferred by this module.
"""
from __future__ import annotations

import copy
import json
import re


_INTEGER = re.compile(r"[1-9][0-9]{0,8}\Z")
_TABLE = r"(?:(?<![A-Za-z0-9])BOM(?![A-Za-z0-9])|\bbill\s+of\s+materials\b|物料清单|物料明细表|零件明细表)"
_ROW = re.compile(_TABLE + r"\s*(?:表\s*)?(?:第\s*([1-9][0-9]{0,8})\s*行|(?:row|item)\s*#?\s*([1-9][0-9]{0,8})(?![0-9]))", re.I)
_UNREADABLE = re.compile(r"无法辨认|无法读取|未读清|unreadable|\?{2,}|\[\s*unknown\s*\]", re.I)


def _source_row_identity(observation):
    if not isinstance(observation, dict):
        return None
    evidence = observation.get("evidence")
    if (not isinstance(evidence, dict) or set(evidence) != {"reference", "page", "region"}
            or not isinstance(evidence["reference"], str) or not evidence["reference"].strip()
            or evidence["reference"] == "inquiry"
            or type(evidence["page"]) is not int or not 1 <= evidence["page"] <= 10000
            or not isinstance(evidence["region"], str)):
        return None
    row_numbers = {int(match.group(1) or match.group(2)) for match in _ROW.finditer(evidence["region"])}
    if len(row_numbers) != 1:
        return None
    return evidence["reference"], evidence["page"], next(iter(row_numbers))


def parse_bom_row(observation):
    """Return a pending row only for four explicit columns and its named row.

    The original observation must have a validated drawing reference/page and
    an evidence region explicitly identifying BOM row N. N must agree with
    the first cell. Unit uncertainty elsewhere in the description does not
    change an explicit integer in the QTY cell. Flagged conflicts, unreadable
    cells, multi-row text and non-integer quantities are not adopted.
    """
    identity = _source_row_identity(observation)
    if identity is None or observation.get("status") not in ("observed", "unit_unknown"):
        return None
    evidence = observation["evidence"]
    text = observation.get("raw_text")
    if (not isinstance(text, str) or not text.strip() or len(text) > 10000
            or len(text.strip().splitlines()) != 1 or _UNREADABLE.search(text)):
        return None
    cells = [cell.strip() for cell in text.strip().strip("|").split("|")]
    if (len(cells) != 4 or not all(cells) or not _INTEGER.fullmatch(cells[0])
            or not _INTEGER.fullmatch(cells[3]) or len(cells[1]) > 128 or len(cells[2]) > 4000
            or int(cells[0]) != identity[2]):
        return None
    result = {"item_no": int(cells[0]), "part_number": cells[1], "description": cells[2],
              "quantity": int(cells[3]), "quantity_scope": "source_bom_row",
              "evidence": copy.deepcopy(evidence), "status": "pending_review", "raw_text": text}
    if observation.get("source_pass") in ("initial", "review", "legacy"):
        result["source_pass"] = observation["source_pass"]
    return result


def project_bom(part):
    """Aggregate one source BOM row, retaining all explicit reading variants.

    Assemblies containing several BOM rows return None. A disagreement in a
    part number does not erase an independently consistent row quantity;
    both the overall status and each relevant field's status stay explicit.
    """
    if (not isinstance(part, dict) or not isinstance(part.get("observations"), list)
            or not isinstance(part.get("evidence"), list)):
        return None
    allowed = {(row.get("reference"), row.get("page")) for row in part.get("evidence", [])
               if isinstance(row, dict)}
    rows, unresolved, seen = [], [], set()
    for observation in part["observations"]:
        row = parse_bom_row(observation)
        if row is None:
            source_row = _source_row_identity(observation)
            if source_row is not None and source_row[:2] in allowed:
                unresolved.append((source_row, copy.deepcopy(observation)))
            continue
        if (row["evidence"]["reference"], row["evidence"]["page"]) not in allowed:
            continue
        identity = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if identity not in seen:
            rows.append(row)
            seen.add(identity)
    identities = {(row["evidence"]["reference"], row["evidence"]["page"], row["item_no"]) for row in rows}
    if len(identities) != 1:
        return None
    reference, page, item_no = next(iter(identities))
    unresolved = [row for identity, row in unresolved if identity == (reference, page, item_no)]
    numbers = list(dict.fromkeys(row["part_number"] for row in rows))
    quantities = list(dict.fromkeys(row["quantity"] for row in rows))
    descriptions = list(dict.fromkeys(row["description"] for row in rows))
    evidence = []
    for row in rows:
        if row["evidence"] not in evidence:
            evidence.append(copy.deepcopy(row["evidence"]))
    issues = []
    if len(numbers) > 1:
        issues.append("同一来源 BOM 行的料号识别存在差异，请对照原表核对。")
    if len(quantities) > 1:
        issues.append("同一来源 BOM 行的用量识别存在差异，未选择采用值。")
    if len(descriptions) > 1:
        issues.append("同一来源 BOM 行的描述有多个转录版本，请对照原表核对。")
    if unresolved:
        issues.append("同一来源 BOM 行另有未能解析或已标记冲突的记录，暂不采用料号及用量。")
    return {"kind": "bom_item", "item_no": item_no, "reference": reference, "page": page,
            "part_number": numbers[0] if len(numbers) == 1 and not unresolved else None, "part_numbers": numbers,
            "description": descriptions[0] if len(descriptions) == 1 else None, "descriptions": descriptions,
            "quantity": quantities[0] if len(quantities) == 1 and not unresolved else None, "quantities": quantities,
            "quantity_scope": "source_bom_row", "status": "conflict" if issues else "pending_review",
            "identity_status": "conflict" if len(numbers) > 1 or unresolved else "pending_review",
            "quantity_status": "conflict" if len(quantities) > 1 or unresolved else "pending_review",
            "evidence": evidence, "rows": rows, "unresolved_rows": unresolved, "warnings": issues}
