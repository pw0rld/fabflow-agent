"""Conservative display corrections over saved text; never rewrite evidence."""
from __future__ import annotations

import copy
import re
import unicodedata


AUTO_CONFLICT_REASON = '两轮规范候选不一致，保留各自原始证据，待人工核对。'
_CHAMFER = re.compile(r'(?<![\w.])\d+(?:\.\d+)?\s*[x×]\s*45\s*[°º]', re.I)


def surface_tokens(value):
    """Recognize only an explicit painted/RAL expression, not arbitrary finishes."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize('NFKC', value).upper()
    text = re.sub(r'标题栏|TREATMENT|FINISH', ' ', text)
    tokens = re.findall(r'PAINTED|RAL\s*\d{4}(?!\d)', text)
    rest = re.sub(r'PAINTED|RAL\s*\d{4}(?!\d)', ' ', text)
    if not tokens or re.sub(r'[\s:;,/、；，：]+', '', rest):
        return None
    normalized = frozenset(re.sub(r'\s+', '', token) for token in tokens)
    if len([token for token in normalized if token.startswith('RAL')]) > 1:
        return None
    return normalized


def compatible_surface(first, second):
    """A same-source shorter description may omit color, but not contradict it."""
    if first.get('name') != 'surface_treatment' or second.get('name') != 'surface_treatment':
        return False
    a, b = first.get('evidence', {}), second.get('evidence', {})
    if (a.get('reference'), a.get('page')) != (b.get('reference'), b.get('page')):
        return False
    left, right = surface_tokens(first.get('value')), surface_tokens(second.get('value'))
    return bool(left and right and (left <= right or right <= left))


def display_observations(rows):
    """Keep raw text/provenance and annotate narrow, inspectable display fixes."""
    result = copy.deepcopy(rows)
    groups = {}
    for row in result:
        if row.get('name') == 'surface_treatment' and row.get('status') == 'conflict':
            evidence = row.get('evidence', {})
            groups.setdefault((evidence.get('reference'), evidence.get('page')), []).append(row)
        raw = row.get('raw_text', '')
        category = ('折弯表' if re.search(r'\bBENDING\s+TABLE\b', raw, re.I) else
                    '倒角标注' if _CHAMFER.search(raw) and not re.search(r'\bbend|折弯|弯曲', raw, re.I) else None)
        if category and row.get('label') != category:
            row['source_label'] = row.get('label') or row.get('source_name')
            row['label'] = category
            row['classification_note'] = '显示分类按原文标注整理，原始模型标签保留，工程含义仍待核对。'
    for group in groups.values():
        # Only undo this application's known automatic two-pass conflict flag.
        # Explicit model conflicts, different colors and unknown finish words stay.
        if (not {'initial', 'review'} <= {row.get('source_pass') for row in group}
                or any(row.get('reason') != AUTO_CONFLICT_REASON for row in group)):
            continue
        tokens = [surface_tokens(row.get('raw_text')) for row in group]
        if not all(tokens):
            continue
        richest = max(tokens, key=len)
        if not all(value <= richest for value in tokens):
            continue
        for row in group:
            row['status'] = 'observed'
            row['reason'] = '两轮为同一涂装的简略与完整表述，原文均保留，仍待人工核对。'
    return result
