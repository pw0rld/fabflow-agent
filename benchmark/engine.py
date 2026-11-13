"""Offline platform-result adapter and scoped benchmark evaluator. Never calls services."""
import argparse
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

def validate(data, name):
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).parent / 'schemas' / (name + '.schema.json')).read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(data)


def iou(a, b):
    x = max(0, min(a[2], b[2])-max(a[0], b[0]))
    y = max(0, min(a[3], b[3])-max(a[1], b[1]))
    intersection = x*y
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1])-intersection
    return intersection/union if union > 0 else 0


def score_collection(actual, spec):
    # Maximum bipartite matching avoids order-dependent greedy pairing.
    expected = spec['items']
    def matches(a, b):
        if any(a.get(k) != b.get(k) for k in spec['keys']):
            return False
        if spec.get('iou_threshold') is not None and iou(a['bbox'], b['bbox']) < spec['iou_threshold']:
            return False
        return all(compare(a.get(k), b.get(k), {'comparison':'numeric', 'tolerance':t})
                   for k,t in spec.get('tolerances', {}).items())
    edges = [[j for j,b in enumerate(expected) if matches(a,b)] for a in actual]
    owners = {}
    def augment(i, seen):
        for j in edges[i]:
            if j in seen: continue
            seen.add(j)
            if j not in owners or augment(owners[j], seen):
                owners[j] = i
                return True
        return False
    for i in range(len(actual)): augment(i, set())
    tp = len(owners); fn = len(expected)-tp
    fp = len(actual)-tp if spec['coverage']=='exhaustive' else None
    return {'matched':tp, 'missing':fn, 'extra':fp,
            'recall':tp/len(expected) if expected else None,
            'precision':tp/(tp+fp) if fp is not None and tp+fp else None,
            'f1':2*tp/(2*tp+fp+fn) if fp is not None and 2*tp+fp+fn else None}

TRACKS = ('regions', 'parameters', 'ownership', 'bom', 'geometry', 'calculation')

def adapt(data, kind):
    if kind == 'normalized':
        validate(data, 'prediction')
        return data
    if kind == 'bom_export':
        rows = data['rows']
        result = adapt({}, 'image')
        result['raw'] = data
        result['bom'] = rows
        result['limitations'] = ['Browser-exported rows retained; no inferred quantities']
        return result
    if kind == 'step_manufacturing':
        result = adapt({}, 'image')
        result['raw'] = data
        for key in ('parts', 'ownership', 'bom', 'geometry', 'calculation'):
            result[key] = data.get(key, [])
        result['limitations'] = ['Requires explicit saved manufacturing export; no mesh conversion']
        return result
    if kind == 'image':
        source = data.get('manifest', data)
        return {**{k: source.get(k, []) for k in ('regions', 'parameters', 'parts')},
                'ownership': source.get('part_assignments', []) + [{'source_id': r['id'], 'part_id': r.get('part_id'), 'source_type': 'region'} for r in source.get('regions', [])],
                'bom': [], 'geometry': [], 'calculation': [],
                'limitations': ['BOM browser projection is not reconstructed by this adapter',
                                 'No inferred manufacturing quantities or geometry'],
                'raw': data}
    if kind == 'step_preview':
        return {'regions': [], 'parameters': [], 'ownership': [], 'bom': [],
                'geometry': [], 'calculation': [], 'parts': [],
                'diagnostics': {'display_mesh_count': len(data.get('meshes', [])),
                                'unit': data.get('unit')},
                'limitations': ['Preview meshes are not manufacturing parts; geometry truth unavailable'],
                'raw': data}
    raise ValueError('Unsupported platform export: ' + kind)

def resolve(data, path):
    for segment in path.split('.'):
        data = data[int(segment)] if isinstance(data, list) else data[segment]
    return data

def compare(actual, expected, rule):
    if rule['comparison'] == 'numeric':
        if actual is None or expected is None: return False
        if isinstance(actual, bool) or isinstance(expected, bool):
            return False
        try:
            a, b = Decimal(str(actual)), Decimal(str(expected))
        except InvalidOperation:
            return False
        return a.is_finite() and b.is_finite() and abs(a-b) <= Decimal(str(rule.get('tolerance', 0)))
    if rule['comparison'] == 'contains':
        return isinstance(actual, str) and isinstance(expected, str) and expected in actual
    return type(actual) is type(expected) and actual == expected

def evaluate(prediction, reference):
    results = []
    for rule in reference['assertions']:
        result = {'id': rule['id'], 'track': rule['track'], 'truth_status': rule['status'],
                  'evidence': rule['evidence']}
        if rule['status'] not in ('engineering_verified', 'provisional'):
            result['verdict'] = 'excluded_pending_adjudication'
        else:
            try:
                actual = resolve(prediction, rule['path'])
                result['actual'] = actual
                result['verdict'] = ('missing' if actual is None else
                                     'matched' if compare(actual, rule['expected'], rule) else 'wrong')
            except (KeyError, IndexError):
                result['verdict'] = 'missing'
        results.append(result)
    tracks = {}
    for track in TRACKS:
        tracks[track] = {}
        for tier, status in [('official', 'engineering_verified'), ('development', 'provisional')]:
            rows = [r for r in results if r['track'] == track and r['truth_status'] == status]
            counts = {v: sum(r['verdict'] == v for r in rows) for v in ('matched', 'missing', 'wrong')}
            tracks[track][tier] = {**counts, 'denominator': len(rows),
                'assertion_accuracy': counts['matched']/len(rows) if rows else None,
                'status': 'scored_partial_reference' if rows else 'not_scorable'}
    collections = []
    for spec in reference.get('collections', []):
        if spec['status'] not in ('provisional', 'engineering_verified'):
            collections.append({'id':spec['id'], 'status':'excluded_pending_adjudication'})
            continue
        collections.append({'id':spec['id'], 'track':spec['track'], 'truth_status':spec['status'], **score_collection(prediction[spec['track']], spec)})
    return {'collections':collections, 'tracks': tracks, 'checks': results, 'model_called': False,
            'limitations': ['Scoped assertions only; no exhaustive false-positive or omission metric',
                           'No overall engineering accuracy; provisional scores are diagnostic']}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True, type=Path)
    p.add_argument('--reference', required=True, type=Path)
    p.add_argument('--kind', choices=['image', 'step_preview', 'bom_export', 'step_manufacturing', 'normalized'], required=True)
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    if a.output.resolve() in (a.input.resolve(), a.reference.resolve()):
        raise ValueError('Output must not overwrite input/reference')
    prediction = adapt(json.loads(a.input.read_text()), a.kind)
    reference = json.loads(a.reference.read_text())
    validate(reference, 'ground-truth')
    validate(prediction, 'prediction')
    report = evaluate(prediction, reference)
    report['input_sha256'] = hashlib.sha256(a.input.read_bytes()).hexdigest()
    report['reference_sha256'] = hashlib.sha256(a.reference.read_bytes()).hexdigest()
    report['adapter'] = a.kind
    report['adapter_limitations'] = prediction['limitations']
    report['diagnostics'] = prediction.get('diagnostics', {})
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')

if __name__ == '__main__':
    main()
