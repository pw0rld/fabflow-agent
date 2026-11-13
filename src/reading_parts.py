"""Evidence-backed drawing parts, independent from quotation or CAD geometry.

The model only links observation indices. This module copies validated source
observations into each part and never infers parts, quantities or parameters.
"""
from __future__ import annotations

import copy
import json
import re

from part_visuals import validate_view
from reading_bom import parse_bom_row


MAX_PARTS = 100
MAX_PART_OBSERVATIONS = 2048
MAX_PART_LINKS = 8192
MAX_PART_EVIDENCE = 2048
MAX_PART_VIEWS = 12
PART_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
MODEL_KEYS = {"key", "name", "part_number", "version", "observation_indices", "evidence"}
PART_KEYS = {"key", "name", "part_number", "version", "status", "observations", "evidence"}
OBSERVATION_KEYS = {"name", "raw_text", "raw_unit", "status", "evidence", "reason"}


def validate_saved_views(views, allowed_pages, *, mapper=None, max_views=2 * MAX_PART_VIEWS):
    """Keep optional, bounded display coordinates independent of part parsing.

    Positions describe the original page, never manufactured CAD geometry.
    An invalid optional view is isolated rather than losing source text or
    failing the drawing run. The caller owns the current part's exact page set.
    """
    if not isinstance(views, list) or len(views) > max_views:
        return []
    references = {reference for reference, _ in allowed_pages}
    result, seen = [], set()
    for value in views:
        try:
            view = validate_view(value)
            if mapper is not None:
                reference, page = mapper(view["reference"], view["page"])
                view.update(reference=reference, page=page)
            view = validate_view(view, references=references)
            if (view["reference"], view["page"]) not in allowed_pages:
                continue
            identity = _evidence_key(view)
            if identity not in seen:
                seen.add(identity)
                result.append(view)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return result


def _view_kind(label):
    """Broad view families; a label alone never establishes a match."""
    text = re.sub(r"[\s_\-]+", "", label).casefold()
    if re.search(r"isometric|axonometric|轴测|等轴|立体", text):
        return "isometric"
    if re.search(r"flatpattern|unfold|展开", text):
        return "flat"
    if re.search(r"section|剖视|剖面|截面", text):
        return "section:" + text
    if re.search(r"detail|局部|详图|放大", text):
        return "detail:" + text
    if re.search(r"top|bottom|front|back|rear|side|俯视|仰视|主视|正视|正面|侧视|侧面|后视|视图", text):
        return "orthographic"
    return "label:" + text


def _same_view_region(first, second):
    if ((first["reference"], first["page"]) != (second["reference"], second["page"])
            or _view_kind(first["label"]) != _view_kind(second["label"])):
        return False
    ax, ay, aw, ah = first["bbox"]
    bx, by, bw, bh = second["bbox"]
    overlap = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    union = aw * ah + bw * bh - overlap
    return (overlap / union >= 0.85
            and abs((ax + aw / 2) - (bx + bw / 2)) <= 0.08 * min(aw, bw)
            and abs((ay + ah / 2) - (by + bh / 2)) <= 0.08 * min(ah, bh))


def _merge_views(first, second, allowed_pages):
    """Join near-identical crops, preserving their complete visible envelope.

    This joins display regions only, never CAD geometry. Distinct source pages,
    incompatible view families and adjacent or nested detail regions remain
    separate. The original pass records are unchanged.
    """
    result = []
    for view in validate_saved_views(first + second, allowed_pages):
        matching = [item for item in result if _same_view_region(item, view)]
        if len(matching) != 1:
            result.append(view)
        else:
            target = matching[0]
            ax, ay, aw, ah = target["bbox"]
            bx, by, bw, bh = view["bbox"]
            x, y = min(ax, bx), min(ay, by)
            target["bbox"] = [x, y, max(ax + aw, bx + bw) - x, max(ay + ah, by + bh) - y]
            target["hotspots"] = _unique(target["hotspots"] + view["hotspots"], _evidence_key)[:16]
    return result


def _text(value, limit, *, nullable=False):
    return (nullable and value is None) or (
        isinstance(value, str) and 1 <= len(value) <= limit
        and value == value.strip() and value.isprintable())


def _identity(row):
    fields = ("name", "source_name", "raw_text", "raw_unit", "status", "evidence")
    return json.dumps({name: row[name] for name in fields if name in row},
                      ensure_ascii=False, sort_keys=True, allow_nan=False)


def _unique(rows, key):
    seen, result = set(), []
    for row in rows:
        identity = key(row)
        if identity not in seen:
            seen.add(identity)
            result.append(copy.deepcopy(row))
    return result


def _evidence_key(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _metadata(item):
    if (not isinstance(item, dict) or not isinstance(item.get("key"), str)
            or not PART_KEY.fullmatch(item["key"])
            or not _text(item.get("name"), 256)
            or not _text(item.get("part_number"), 128, nullable=True)
            or not _text(item.get("version"), 128, nullable=True)):
        raise ValueError("part must have a bounded key, name, part number and version")


def _mapped_evidence(row):
    if (not isinstance(row, dict) or set(row) != {"reference", "page", "region"}
            or not _text(row["reference"], 128) or row["reference"] == "inquiry"
            or type(row["page"]) is not int or not 1 <= row["page"] <= 10000
            or not isinstance(row["region"], str) or not row["region"].strip()
            or len(row["region"]) > 1000):
        raise ValueError("invalid normalized part evidence")


def _observation(row, references, *, allow_unlinked=False):
    if (not isinstance(row, dict) or not OBSERVATION_KEYS <= set(row)
            or set(row) - OBSERVATION_KEYS - {"source_name", "source_pass"}
            or not _text(row["name"], 128)
            or not isinstance(row["raw_text"], str) or not row["raw_text"].strip()
            or len(row["raw_text"]) > 10000
            or not isinstance(row["status"], str)
            or row["status"] not in {"observed", "unit_unknown", "conflict", "unreadable"}
            or not isinstance(row["evidence"], dict)
            or not isinstance(row["evidence"].get("reference"), str)
            or any(row[name] is not None and (not isinstance(row[name], str) or len(row[name]) > 4000)
                   for name in ("raw_unit", "reason"))
            or ("source_name" in row and not _text(row["source_name"], 128))
            or ("source_pass" in row and (not isinstance(row["source_pass"], str)
                                          or row["source_pass"] not in {"initial", "review", "legacy"}))):
        raise ValueError("part observation must retain validated source text and unconfirmed state")
    _mapped_evidence(row["evidence"])
    # Validate every field before considering a recoverable source mismatch.
    # An invalid status or location must never be hidden by a wrong part link.
    if row["evidence"]["reference"] not in references:
        if allow_unlinked:
            return False
        raise ValueError("part observation must retain validated source text and unconfirmed state")
    return True


def validate_model_parts(parts, observations, evidence_validator, *, skipped=None, view_mapper=None):
    """Validate model groups against already-mapped source observations.

    ``evidence_validator`` is the bridge's current-run evidence callback. A
    linked observation must belong to one of that part's original references;
    this check deliberately runs after PDF/detail-image reference mapping.
    With an explicit ``skipped`` list, only otherwise-valid observations linked
    outside a part's declared sources are left unassigned and recorded there.
    Source evidence is never expanded and the original observation is intact.
    """
    if not isinstance(parts, list) or len(parts) > MAX_PARTS:
        raise ValueError("bounded parts list required")
    if not isinstance(observations, list):
        raise ValueError("parts require the original observation list")
    if skipped is not None and not isinstance(skipped, list):
        raise ValueError("skipped part links require a list")
    result, seen, links = [], set(), 0
    for item in parts:
        if (not isinstance(item, dict) or not MODEL_KEYS <= set(item)
                or set(item) - MODEL_KEYS - {"views"}):
            raise ValueError("unsupported model part shape")
        _metadata(item)
        if item["key"] in seen:
            raise ValueError("duplicate part key")
        seen.add(item["key"])
        indices = item["observation_indices"]
        if (not isinstance(indices, list) or len(indices) > MAX_PART_OBSERVATIONS
                or any(type(index) is not int or not 0 <= index < len(observations) for index in indices)
                or len(indices) != len(set(indices))):
            raise ValueError("part observation indices must be unique current observation positions")
        links += len(indices)
        if links > MAX_PART_LINKS:
            raise ValueError("too many part observation links")
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_PART_EVIDENCE:
            raise ValueError("part requires bounded source evidence")
        mapped = _unique([evidence_validator(row) for row in evidence], _evidence_key)
        for row in mapped:
            _mapped_evidence(row)
        references = {row["reference"] for row in mapped}
        linked = []
        for index in indices:
            observation = observations[index]
            if not _observation(observation, references, allow_unlinked=skipped is not None):
                skipped.append({"part_key": item["key"], "observation_index": index,
                                "reference": observation["evidence"]["reference"]})
                continue
            linked.append(copy.deepcopy(observation))
        result.append({name: copy.deepcopy(item[name]) for name in
                       ("key", "name", "part_number", "version")})
        result[-1].update(status="pending_review", evidence=mapped, observations=linked)
        if "views" in item:
            pages = {(row["reference"], row["page"]) for row in mapped}
            result[-1]["views"] = validate_saved_views(item["views"], pages,
                                                       mapper=view_mapper, max_views=MAX_PART_VIEWS)
    return result


def _checked_parts(parts, source_pass):
    multiplier = 2 if source_pass is None else 1
    if not isinstance(parts, list) or len(parts) > multiplier * MAX_PARTS:
        raise ValueError("bounded normalized parts list required")
    result, keys, links = [], set(), 0
    for item in parts:
        if (not isinstance(item, dict) or not PART_KEYS <= set(item)
                or set(item) - PART_KEYS - {"views"}):
            raise ValueError("unsupported normalized part shape")
        _metadata(item)
        if item["key"] in keys or item["status"] != "pending_review":
            raise ValueError("normalized parts must have unique keys and remain pending review")
        keys.add(item["key"])
        evidence, observations = item["evidence"], item["observations"]
        if (not isinstance(evidence, list) or not 1 <= len(evidence) <= multiplier * MAX_PART_EVIDENCE
                or not isinstance(observations, list) or len(observations) > multiplier * MAX_PART_OBSERVATIONS):
            raise ValueError("bounded normalized part evidence and observations required")
        for row in evidence:
            _mapped_evidence(row)
        references = {row["reference"] for row in evidence}
        part = copy.deepcopy(item)
        part["evidence"] = _unique(evidence, _evidence_key)
        if "views" in part:
            part["views"] = validate_saved_views(part["views"],
                                                 {(row["reference"], row["page"]) for row in evidence})
        part["observations"] = []
        for row in observations:
            checked_row = row
            if source_pass is None and isinstance(row, dict) and "label" in row:
                if not _text(row["label"], 256):
                    raise ValueError("invalid projected observation label")
                checked_row = {name: value for name, value in row.items() if name != "label"}
            _observation(checked_row, references)
            observed = copy.deepcopy(row)
            if source_pass is not None:
                observed["source_pass"] = source_pass
            part["observations"].append(observed)
        links += len(observations)
        if links > multiplier * MAX_PART_LINKS:
            raise ValueError("too many normalized part observation links")
        result.append(part)
    return result


def _version_identity(value):
    if value is None:
        return None
    match = re.fullmatch(r"(?:REV(?:ISION)?\.?\s*:?\s*)?([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)", value, re.I)
    return match.group(1).upper() if match else value


def _number_without_matching_revision(number, version):
    """Recognize only an explicit trailing revision matching its own version.

    This comparison alias never changes the displayed part number. A bare
    trailing number, an unknown version or a contradictory suffix stays exact.
    """
    if number is None or version is None:
        return number
    match = re.fullmatch(r"(.+?)[ _-]*(?:REVISION|REV)[ _-]*([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)",
                         number, re.I)
    if match and _version_identity(match.group(2)) == _version_identity(version):
        base = match.group(1).rstrip(" _-")
        if base:
            return base
    return number


def _bom_identity(part):
    """A table row is a narrow identity only on its exact validated page."""
    rows = [value for row in part["observations"] if (value := parse_bom_row(row)) is not None]
    identities = {(row["evidence"]["reference"], row["evidence"]["page"], row["item_no"]) for row in rows}
    allowed = {(row["reference"], row["page"]) for row in part["evidence"]}
    if (len(identities) != 1 or not rows or not part.get("part_number")
            or not any(row["part_number"] == part["part_number"] for row in rows)
            or any((row["evidence"]["reference"], row["evidence"]["page"]) not in allowed for row in rows)):
        return None
    return next(iter(identities)), {row.get("source_pass") for row in rows}


def _same_part(first, second):
    if _version_identity(first["version"]) != _version_identity(second["version"]):
        return False
    first_row, second_row = _bom_identity(first), _bom_identity(second)
    if first_row is not None and second_row is not None:
        if first_row[0] != second_row[0]:
            return False
        # A same-row OCR difference is not a globally interchangeable part
        # number. It only joins observations from the two actual passes.
        if (("initial" in first_row[1] and second_row[1] == {"review"})
                or ("review" in first_row[1] and second_row[1] == {"initial"})):
            return True
    if first["part_number"] is not None or second["part_number"] is not None:
        identity = first["part_number"] is not None and first["part_number"] == second["part_number"]
        if not identity and first["part_number"] is not None and second["part_number"] is not None:
            # A revision suffix is an alias only with the same name and the
            # complete same source set. Shared summary files alone cannot join
            # two different drawings. Exact-number matches keep their old rule.
            first_number = _number_without_matching_revision(first["part_number"], first["version"])
            second_number = _number_without_matching_revision(second["part_number"], second["version"])
            identity = (first_number == second_number and first["name"] == second["name"]
                        and {row["reference"] for row in first["evidence"]}
                        == {row["reference"] for row in second["evidence"]})
    else:
        identity = first["name"] == second["name"]
    return identity and bool({row["reference"] for row in first["evidence"]}
                             & {row["reference"] for row in second["evidence"]})


def _combine_parts(parts):
    result = []
    for part in parts:
        matching = [item for item in result if _same_part(item, part)]
        if len(matching) == 1:
            destination = matching[0]
            destination["observations"] = _unique(destination["observations"] + part["observations"], _identity)
            destination["evidence"] = _unique(destination["evidence"] + part["evidence"], _evidence_key)
            if "views" in destination or "views" in part:
                pages = {(row["reference"], row["page"]) for row in destination["evidence"]}
                destination["views"] = _merge_views(destination.get("views", []), part.get("views", []), pages)
        else:
            taken = {item["key"] for item in result}
            if part["key"] in taken:
                stem, counter = part["key"][:58], 2
                while f"{stem}-{counter}" in taken:
                    counter += 1
                part["key"] = f"{stem}-{counter}"
            result.append(part)
    if (len(result) > 2 * MAX_PARTS
            or sum(len(part["observations"]) for part in result) > 2 * MAX_PART_LINKS
            or any(len(part["observations"]) > 2 * MAX_PART_OBSERVATIONS
                   or len(part["evidence"]) > 2 * MAX_PART_EVIDENCE for part in result)):
        raise ValueError("merged parts exceed two-pass limits")
    return result


def merge_parts(initial, reviewed=None):
    """Merge two real passes without guessing identities from similar names.

    Explicit same-page BOM row identity can join conflicting OCR part numbers;
    both original observations remain pending and project_bom exposes variants.
    """
    return _combine_parts(_checked_parts(initial, "initial")
                          + _checked_parts([] if reviewed is None else reviewed, "review"))


def reconcile_parts(parts):
    """Read-only reconciliation of validated saved or projected part records.

    No pass is relabeled and no historical file changes. This lets existing
    completed runs benefit from conservative row and view reconciliation.
    """
    result = _combine_parts(_checked_parts(parts, None))
    for part in result:
        if "views" in part:
            pages = {(row["reference"], row["page"]) for row in part["evidence"]}
            part["views"] = _merge_views(part["views"], [], pages)
    return result
