"""Read verbatim XCAF physical-material assignments, without grade inference.

The material table alone is not evidence that a solid uses a material. Only
actual MaterialRef tree links on its definition, occurrence or owned subshape
are followed. Partial or conflicting assignments remain unresolved.
"""
from __future__ import annotations

import re

MAX_ENTRIES = 128
MAX_LABELS = 4096
MAX_NAME = 512
MAX_DESCRIPTION = 2000


def is_placeholder_material_name(value):
    """Recognize exact template/property names, never a material from a file name."""
    if not isinstance(value, str):
        return False
    compact = re.sub(r'[\s_\-:：/\\()（）]+', '', value).upper()
    return compact in {'COMPANY', 'DESIGNCOMPANY', '设计单位', '设计公司', '公司名称',
                       'PTCMASTERMATERIAL', 'PTCSYSTEMMTRLPROPS', 'PTCSYSTEMMATERIAL',
                       'PTCMATERIALNAME', 'MATERIALNAME', '材料名称', '材质名称'}


_STAINLESS = r'(?:201|202|301|302|303|304L?|309S?|310S?|316(?:L|TI)?|317L?|321|347|409|410|420|430|439|441|444|904L|2205|2507)'
_ALUMINIUM = r'(?:5052(?:[\s-]*H32)?|6061(?:[\s-]*T6)?|6063(?:[\s-]*T5)?)'
_BOUND_START, _BOUND_END = r'(?<![A-Z0-9])', r'(?![A-Z0-9])'


def _grade_tokens(text):
    """A bounded vocabulary of explicit grade tokens in a material attribute.

    Bare alloy numbers require the whole field or an explicit material-family
    context. Dimensions, a filename, density, generic Steel and template names
    cannot supply a grade. The matched literal remains available as evidence.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_DESCRIPTION:
        return []
    upper = text.upper()
    found = []
    patterns = [
        (r'(?:SUS|SS|AISI)[\s:-]*' + _STAINLESS, 'stainless'),
        (_STAINLESS + r'[\s:-]*SS', 'stainless'),
        (r'Q(?:195|215|235|255|275|295|345|355|390|420|460)[A-F]?', 'steel'),
        (r'(?:SPCC|SPCD|SPCE|DC0[1-6]|S(?:235|275|355)(?:JR|J0|J2)|ASTM[\s-]*A36|AISI[\s-]*1018)', 'steel'),
        (r'AL(?:UMINIUM|UMINUM)?[\s:-]*' + _ALUMINIUM, 'aluminium'),
    ]
    if re.fullmatch(_STAINLESS, upper.strip()) or re.search(r'不锈钢|STAINLESS', upper):
        patterns.append((_STAINLESS, 'stainless'))
    if re.fullmatch(_ALUMINIUM, upper.strip()) or re.search(r'铝|ALUMINIUM|ALUMINUM', upper):
        patterns.append((_ALUMINIUM, 'aluminium'))
    for pattern, family in patterns:
        for match in re.finditer(_BOUND_START + '(?:' + pattern + ')' + _BOUND_END, upper):
            literal = text[match.start():match.end()].strip()
            key = re.sub(r'[\s:-]+', '', match.group()).upper()
            if family == 'stainless':
                key = re.sub(r'^(?:SUS|SS|AISI)', '', key)
                key = re.sub(r'SS$', '', key)
            elif family == 'aluminium':
                key = re.sub(r'^AL(?:UMINIUM|UMINUM)?', '', key)
            identity = (family + ':' + key, literal)
            if identity not in found:
                found.append(identity)
    # A description may say "Material grade: 304" without repeating the
    # stainless family. Keep the labelled token, not unrelated dimensions.
    contextual = (r'(?:MATERIAL(?:\s+GRADE)?|GRADE|材质|材料|牌号)[\s:：=]+'
                  + '(' + _STAINLESS + ')' + _BOUND_END)
    for match in re.finditer(contextual, upper):
        literal = text[match.start(1):match.end(1)]
        identity = ('stainless:' + match.group(1), literal)
        if identity not in found:
            found.append(identity)
    return found


def material_grade_candidate(material):
    """Separate a specific grade from verbatim XCAF material name/description.

    This reads only assignment-backed source attributes, including old cached
    records whose template name was historically marked candidate. It cannot
    change an adopted pricing profile or guess 304/316 from generic stainless.
    """
    from cad_bom import validate_material
    result = {'status': 'unresolved', 'value': None, 'grade_key': None, 'evidence': [],
              'reason': 'CAD材料属性未提供可识别的具体牌号；原文保留，未从文件名推定。'}
    try:
        validate_material(material)
    except (ValueError, TypeError, KeyError):
        return result
    # Unresolved records with warnings may be truncated or incompletely read.
    # A template-only name is unresolved with its complete original entries.
    if material['status'] == 'unresolved' and material['warnings']:
        result['reason'] = '原始材料来源不完整，未采用部分文本补出牌号。'
        return result
    entries = material['entries']
    direct = [row for row in entries if row['scope'] in {'definition', 'occurrence'}]
    # A candidate already establishes a full subshape assignment. Unresolved
    # subshape-only/inherited evidence cannot establish the whole solid.
    scoped = [row for row in entries if row['scope'] != 'inherited'] if material['status'] == 'candidate' else direct
    if not scoped:
        result['reason'] = '材料仅有上级或未确认完整范围的局部来源，未采用为整件牌号。'
        return result
    keys, all_keys = {}, set()
    for entry in entries:
        for field in ('name', 'description'):
            text = entry[field]
            if field == 'name' and is_placeholder_material_name(text):
                continue
            for key, literal in _grade_tokens(text):
                all_keys.add(key)
                if entry in scoped:
                    keys.setdefault(key, literal)
                result['evidence'].append({'text': text, 'field': field, 'grade': literal,
                    **{name: entry[name] for name in ('assignment_label', 'material_label', 'scope')}})
    if len(all_keys) > 1:
        result.update(status='conflict', reason='同一实体的材料名或描述包含不同具体牌号，未自动选择。')
    elif keys:
        key, literal = next(iter(keys.items()))
        result.update(status='candidate', value=literal, grade_key=key,
                      reason='具体牌号来自整件关联的CAD材料名或描述原文，仍待工程核对。')
    return result


class MaterialReader:
    def __init__(self, document, shape_tool):
        from OCP.XCAFDoc import XCAFDoc
        self.document = document
        self.shape_tool = shape_tool
        self.guid = XCAFDoc.MaterialRefGUID_s()
        self.direct_cache = {}
        self.children_cache = {}

    @staticmethod
    def _entry(label):
        from OCP.TCollection import TCollection_AsciiString
        from OCP.TDF import TDF_Tool
        value = TCollection_AsciiString()
        TDF_Tool.Entry_s(label, value)
        return value.ToCString()

    @staticmethod
    def _attributes(label):
        from OCP.TDF import TDF_AttributeIterator
        values = []
        iterator = TDF_AttributeIterator(label)
        while iterator.More():
            values.append(iterator.Value())
            iterator.Next()
        return values

    def _direct(self, label):
        from OCP.XCAFDoc import XCAFDoc_Material
        key = self._entry(label)
        if key in self.direct_cache:
            return self.direct_cache[key]
        entries, warnings = [], []
        for attribute in self._attributes(label):
            if not attribute.ID().IsSame(self.guid) or not attribute.HasFather():
                continue
            target = attribute.Father().Label()
            material = next((a for a in self._attributes(target) if isinstance(a, XCAFDoc_Material)), None)
            if material is None:
                warnings.append('材料引用未指向有效的原始材料属性。')
                continue
            name = material.GetName().ToCString() if material.GetName() else ''
            description = material.GetDescription().ToCString() if material.GetDescription() else ''
            if len(name) > MAX_NAME or len(description) > MAX_DESCRIPTION:
                warnings.append('材料原文超出本轮读取长度限制，未截断后冒充完整材料。')
                continue
            if any(ord(char) < 32 and char not in '\n\t' for text in (name, description) for char in text):
                warnings.append('该条材料原文含不支持的控制字符，已保留为本实体待确认，未影响其他实体。')
                continue
            entries.append({'name': name, 'description': description,
                            'material_label': self._entry(target), 'assignment_label': key})
        result = entries, warnings
        self.direct_cache[key] = result
        return result

    def _children(self, label):
        from OCP.TDF import TDF_ChildIterator
        key = self._entry(label)
        if key in self.children_cache:
            return self.children_cache[key]
        result, limited = [], False
        iterator = TDF_ChildIterator(label, True)
        count = 0
        while iterator.More():
            count += 1
            if count > MAX_LABELS:
                limited = True
                break
            child = iterator.Value()
            direct, warnings = self._direct(child)
            if direct or warnings:
                shape = self.shape_tool.GetShape_s(child)
                if not shape.IsNull():
                    result.append((child, shape))
            iterator.Next()
        self.children_cache[key] = result, limited
        return result, limited

    @staticmethod
    def _relation(solid, assigned_shape, maps):
        """(belongs to this solid, covers this entire solid), topology only."""
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
        if solid.IsSame(assigned_shape):
            return True, True
        # A material may belong to a labelled compound containing this solid.
        whole = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
        TopExp.MapShapes_s(assigned_shape, TopAbs_SOLID, whole)
        if whole.Contains(solid):
            return True, True
        kind = assigned_shape.ShapeType()
        if kind not in maps:
            children = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
            TopExp.MapShapes_s(solid, kind, children)
            maps[kind] = children
        return maps[kind].Contains(assigned_shape), False

    def read(self, definition, occurrence, local_solid, ancestors=()):
        """Resolve source links for one original solid occurrence.

        Ancestor assembly labels are retained as inherited evidence, but alone
        do not establish a constituent solid's material. No geometric proximity,
        filename, display name, density or global material-table match is used.
        """
        entries, warnings, full, seen = [], [], set(), set()

        def append(label, scope, covers):
            raw, issues = self._direct(label)
            warnings.extend(issues)
            for item in raw:
                identity = (item['assignment_label'], item['material_label'])
                if identity in seen:
                    continue
                seen.add(identity)
                entry = dict(item, scope=scope)
                entries.append(entry)
                if covers:
                    full.add(identity)

        append(definition, 'definition', True)
        if self._entry(occurrence) != self._entry(definition):
            append(occurrence, 'occurrence', True)
        for label in ancestors:
            if self._entry(label) not in (self._entry(definition), self._entry(occurrence)):
                append(label, 'inherited', False)
        roots = [(definition, local_solid)]
        if self._entry(occurrence) != self._entry(definition):
            located = local_solid.Moved(self.shape_tool.GetLocation_s(occurrence))
            roots.append((occurrence, located))
        for parent, solid in roots:
            children, limited = self._children(parent)
            if limited:
                warnings.append('子形状标签超出本轮材料读取预算。')
            maps = {}
            for child, assigned in children:
                belongs, covers = self._relation(solid, assigned, maps)
                if belongs:
                    append(child, 'subshape', covers)
        # Ordering is deterministic; identical material table rows from another
        # instance never participate unless that instance actually refers here.
        result = {'status': 'unresolved', 'value': None, 'entries': entries[:MAX_ENTRIES],
                  'reason': '当前实体没有可确认归属的原始材料名称。',
                  'warnings': list(dict.fromkeys(warnings))}
        if warnings or len(entries) > MAX_ENTRIES:
            result['reason'] = '原始材料引用不完整或超出读取预算，保留待确认。'
            if len(entries) > MAX_ENTRIES:
                result['warnings'].append('材料来源条目超过本轮上限，未选择其中一项代替全部。')
            return result
        if not entries:
            return result
        names = {e['name'] for e in entries}
        if len(names) != 1 or not next(iter(names)).strip():
            result['reason'] = '同一实体存在不同或空白材料原文，未自动选择牌号。'
            return result
        if not full:
            result['reason'] = '材料仅标在局部子形状或上级装配，不能据此确定整个实体材料。'
            return result
        if all(is_placeholder_material_name(name) for name in names):
            result['reason'] = '原始材料名称为模板或通用属性字段，未作为有效材料；原文与赋值来源已保留。'
            return result
        result.update(status='candidate', value=next(iter(names)),
                      reason='已读取直接关联此实体的原始材料名称，尚未人工确认。',
                      warnings=['保留材料原文，未推断 Q235、304 等具体牌号。'])
        return result
