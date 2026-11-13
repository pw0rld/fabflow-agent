"""Preserve bounded original STEP product entity subgraphs byte for byte.

Used only after ordinary BRep export fails its strict reimport checks. Original
product roots are enumerated without names or guessed IDs, and a caller must
find exactly one physically matching single solid by native reimport. This
module never rebuilds surfaces, modifies the original or approves geometry.
"""
import hashlib
import os
from pathlib import Path
import re
import tempfile

MAX_BYTES = 20*1024*1024
MAX_ENTITIES = 250000
MAX_CANDIDATES = 8
BREP_KINDS = {'MANIFOLD_SOLID_BREP', 'FACETED_BREP', 'BREP_WITH_VOIDS'}
REPRESENTATION_KINDS = {'ADVANCED_BREP_SHAPE_REPRESENTATION', 'FACETED_BREP_SHAPE_REPRESENTATION'}


class SubsetError(ValueError):
    pass


def require(value, message):
    if not value:
        raise SubsetError(message)


def masked(data):
    """Mask strings/comments without changing byte positions or numeric data."""
    output = bytearray(data)
    index = 0
    while index < len(data):
        if data[index:index+2] == b'/*':
            end = data.find(b'*/', index+2)
            require(end >= 0, 'STEP 注释未闭合。')
            output[index:end+2] = b' '*(end+2-index)
            index = end+2
        elif data[index] == 39:
            start = index
            index += 1
            while index < len(data):
                if data[index] != 39:
                    index += 1
                elif data[index:index+2] == b"''":
                    index += 2
                else:
                    index += 1
                    break
            else:
                raise SubsetError('STEP 字符串未闭合。')
            output[start:index] = b' '*(index-start)
        else:
            index += 1
    return bytes(output)


def arguments(expression):
    begin, end = expression.find(b'('), expression.rfind(b')')
    require(begin >= 0 and end > begin, 'STEP 实体参数无效。')
    parts, depth, start = [], 0, begin+1
    for index in range(begin+1, end):
        token = expression[index]
        if token == 40:
            depth += 1
            require(depth <= 128, 'STEP 参数嵌套过深。')
        elif token == 41:
            depth -= 1
            require(depth >= 0, 'STEP 实体括号不平衡。')
        elif token == 44 and depth == 0:
            parts.append(expression[start:index].strip())
            start = index+1
    require(depth == 0, 'STEP 实体括号不平衡。')
    return [*parts, expression[start:end].strip()]


def reference(value):
    found = re.fullmatch(rb'\s*#([1-9][0-9]*)\s*', value)
    return int(found[1]) if found else None


class SourceStepSubset:
    def __init__(self, source):
        path = Path(source)
        require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= MAX_BYTES,
                '原始 STEP 单件子图输入无效。')
        self.data = path.read_bytes()
        self.path = path
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        view = masked(self.data)
        require(re.match(rb'\s*ISO-10303-21\s*;', view, re.I), '原件不是受支持的 STEP P21 文件。')
        self.entities, self.order = {}, []
        begin, in_data, ended = 0, False, False
        self.header = None
        for match in re.finditer(rb';', view):
            end = match.end()
            statement = view[begin:end].strip()
            if statement.upper() == b'DATA;':
                require(self.header is None and not in_data and not ended, '暂不支持多 DATA 段 STEP 子图导出。')
                self.header, in_data = self.data[:end], True
            elif in_data and statement.upper() == b'ENDSEC;':
                in_data, ended = False, True
            elif in_data:
                record = re.fullmatch(rb'\s*#([1-9][0-9]*)\s*=\s*(.+);\s*', view[begin:end], re.S)
                require(record is not None, 'STEP DATA 段存在不受支持的记录。')
                identity = int(record[1])
                require(identity not in self.entities and identity <= 1000000000
                        and len(self.entities) < MAX_ENTITIES, 'STEP 实体编号重复或超出预算。')
                expression = record[2].strip()
                name = re.match(rb'([A-Z][A-Z0-9_]*)\s*\(', expression, re.I)
                kind = name[1].decode('ascii').upper() if name else 'COMPLEX'
                refs = tuple(int(value) for value in re.findall(rb'#([1-9][0-9]*)', expression))
                self.entities[identity] = {'kind': kind, 'expression': expression,
                                           'references': refs, 'raw': self.data[begin:end]}
                self.order.append(identity)
            begin = end
        require(self.header is not None and ended and not in_data and self.entities,
                'STEP DATA 段不完整。')

    def closure(self, root):
        seen, pending = set(), [root]
        while pending:
            identity = pending.pop()
            if identity in seen:
                continue
            require(identity in self.entities, 'STEP 子图存在悬空引用。')
            seen.add(identity)
            pending.extend(self.entities[identity]['references'])
        return seen

    def candidates(self):
        result = []
        for identity in self.order:
            root = self.entities[identity]
            if root['kind'] != 'SHAPE_DEFINITION_REPRESENTATION':
                continue
            fields = arguments(root['expression'])
            if len(fields) != 2:
                continue
            definition, representation = (self.entities.get(reference(value), {}) for value in fields)
            if definition.get('kind') != 'PRODUCT_DEFINITION_SHAPE' or representation.get('kind') not in REPRESENTATION_KINDS:
                continue
            product_fields = arguments(definition['expression'])
            if len(product_fields) != 3 or self.entities.get(reference(product_fields[2]), {}).get('kind') != 'PRODUCT_DEFINITION':
                continue
            rep_fields = arguments(representation['expression'])
            if len(rep_fields) != 3 or not re.fullmatch(rb'\(\s*#[0-9]+(?:\s*,\s*#[0-9]+)*\s*\)', rep_fields[1]):
                continue
            items = [int(value) for value in re.findall(rb'#([0-9]+)', rep_fields[1])]
            breps = [item for item in items if self.entities.get(item, {}).get('kind') in BREP_KINDS]
            if len(breps) != 1 or not all(item == breps[0] or self.entities.get(item, {}).get('kind') == 'AXIS2_PLACEMENT_3D' for item in items):
                continue
            closure = self.closure(identity)
            if {item for item in closure if self.entities[item]['kind'] in BREP_KINDS} != {breps[0]}:
                continue
            result.append({'root': identity, 'brep': breps[0], 'closure': closure})
            require(len(result) <= MAX_CANDIDATES, '原始 STEP 独立单件候选超过保真导出预算。')
        require(result, '原始 STEP 尚无受支持的独立单件产品表示。')
        return result

    def write_matching(self, filename, validate):
        """Publish only after exactly one subset passes caller's native checks."""
        output = Path(filename)
        require(not output.exists() and not output.is_symlink(), '单件 STEP 目标已存在。')
        candidates = self.candidates()
        matches = []
        with tempfile.TemporaryDirectory(prefix='.source-step-subsets-', dir=output.parent) as temporary:
            for index, candidate in enumerate(candidates):
                path = Path(temporary) / ('candidate-' + str(index) + '.step')
                path.write_bytes(self.header + b'\n' + b''.join(self.entities[item]['raw'] for item in self.order
                                 if item in candidate['closure']) + b'\nENDSEC;\nEND-ISO-10303-21;\n')
                try:
                    validation = validate(path)
                except (ValueError, RuntimeError):
                    continue
                require(isinstance(validation, dict), 'STEP 子图几何校验结果无效。')
                matches.append((path, candidate, validation))
            require(len(matches) == 1, '原始 STEP 单件几何未唯一匹配；不按名称或相似度猜选。')
            path, candidate, validation = matches[0]
            require(not self.path.is_symlink() and self.path.stat().st_size == len(self.data)
                    and hashlib.sha256(self.path.read_bytes()).hexdigest() == self.sha256,
                    '原始 STEP 在保真导出期间发生变化。')
            os.replace(path, output)
        return validation, {'method': 'source_step_entity_subset', 'geometry_rebuilt': False,
            'source_sha256': self.sha256, 'source_model_entities': len(self.entities),
            'subset_entities': len(candidate['closure']), 'checked_candidates': len(candidates),
            'source_root_entity': candidate['root'], 'source_brep_entity': candidate['brep']}
