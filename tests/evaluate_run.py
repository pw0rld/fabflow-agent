#!/usr/bin/env python3
"""Compare a saved record with an independently held reference.

This never calls a model and never promotes a fixture comparison into an AI test.
Keep the reference and report outside the Agent's runtime task directory.
"""
from __future__ import annotations
import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys

FORBIDDEN_OUTPUT_KEYS = frozenset({
    'cost_total', 'total_cost', 'order_cost', 'unit_cost', 'subtotal',
    'quote_total', 'quotation_total', 'sale_unit_price', 'selling_price',
    'markup', 'markup_amount', 'approved_quote', 'business_approval',
    'quote_revision', 'recalculated_quote',
})


def resolve(data, path):
    current = data
    for segment in path.split('.'):
        if isinstance(current, list) and segment.isdecimal():
            current = current[int(segment)]
        else:
            current = current[segment]
    return current


def equal(actual, expected, numeric=False):
    if not numeric:
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    try:
        a, b = Decimal(str(actual)), Decimal(str(expected))
        return a.is_finite() and b.is_finite() and a == b
    except (InvalidOperation, ValueError):
        return False


def output_violations(data, prefix=''):
    """Inspect structured output keys, not words in source text or raw rates."""
    violations = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f'{prefix}.{key}' if prefix else key
            if key in FORBIDDEN_OUTPUT_KEYS:
                violations.append(path)
            violations.extend(output_violations(value, path))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            violations.extend(output_violations(value, f'{prefix}.{index}'))
    return violations


def evaluate(record, expected, run_manifest=None):
    results = []
    for assertion in expected.get('assertions', []):
        name, path = assertion['name'], assertion['path']
        try:
            actual = resolve(record, path)
            if 'equals' in assertion:
                passed = equal(actual, assertion['equals'], assertion.get('numeric', False))
            elif 'one_of' in assertion:
                passed = any(equal(actual, candidate) for candidate in assertion['one_of'])
            elif 'contains' in assertion:
                passed = assertion['contains'] in actual
            elif 'absent' in assertion:
                passed = False
            else:
                raise ValueError(f'Unsupported assertion: {name}')
            results.append({'name': name, 'path': path, 'passed': passed,
                            'actual': actual, 'expectation': assertion})
        except (KeyError, IndexError, TypeError) as error:
            results.append({'name': name, 'path': path,
                            'passed': assertion.get('absent') is True,
                            'missing': True, 'error': type(error).__name__})
    violations = output_violations(record)
    results.append({'name': 'four_step_scope', 'passed': not violations,
                    'structured_output_violations': violations})
    declared_kind = (run_manifest or {}).get('run_kind', 'saved_record_only')
    model_artifacts = (run_manifest or {}).get('model_execution', {})
    # A supplied manifest is traceability metadata, not independent proof of an API call.
    has_model_metadata = declared_kind == 'dsh_model' and all(
        model_artifacts.get(key) for key in ('session_id', 'model', 'started_at', 'completed_at', 'log_sha256'))
    return {
        'evaluation_type': 'saved_record_reference_comparison',
        'case_id': expected.get('case_id'),
        'reference_status': expected.get('reference_status', 'unspecified'),
        'checks_passed': all(check['passed'] for check in results),
        'check_count': len(results),
        'limitations': ['只比较参考中列出的字段；未自动审查自由文本是否包含推导费用。',
                        '完整模型输出、工程事实与费用边界仍需人工复核。'],
        'results': results,
        'execution': {
            'declared_run_kind': declared_kind,
            'model_execution': ('declared_with_trace_metadata_not_independently_verified'
                                if has_model_metadata else 'not_evidenced'),
            'model_was_called_by_this_evaluator': False,
            'notice': ('该评估器只比较保存记录；字段通过不等于已完成 DSH/模型端到端测试。'
                       '参考答案仍需独立技术复核。'),
        },
    }


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--expected', type=Path, required=True)
    parser.add_argument('--run-manifest', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        record_path, expected_path, report_path = [p.resolve() for p in
                                                  (args.record, args.expected, args.report)]
        if report_path in (record_path, expected_path):
            raise ValueError('Report must not overwrite record/reference')
        record = json.loads(record_path.read_text(encoding='utf-8'))
        expected = json.loads(expected_path.read_text(encoding='utf-8'))
        manifest = json.loads(args.run_manifest.read_text(encoding='utf-8')) if args.run_manifest else None
        report = evaluate(record, expected, manifest)
        report['inputs'] = {
            'record_sha256': sha256(record_path),
            'reference_sha256': sha256(expected_path),
            'run_manifest_sha256': sha256(args.run_manifest) if args.run_manifest else None,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'checks_passed': report['checks_passed'], 'report': str(report_path),
                          'model_execution': report['execution']['model_execution']}, ensure_ascii=False))
        return 0 if report['checks_passed'] else 1
    except (OSError, ValueError, TypeError) as error:
        print(f'evaluation error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
