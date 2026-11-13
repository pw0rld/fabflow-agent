"""Tests of evaluation meaning, missing values and scope boundaries."""
import unittest
from evaluate_run import evaluate, equal


class EvaluationMeaningTests(unittest.TestCase):
    def expected(self, *assertions):
        return {'case_id': 'independent-case', 'reference_status': 'draft_pending_technical_review',
                'assertions': list(assertions)}

    def test_fixture_success_does_not_claim_a_model_run(self):
        report = evaluate({'quantity': 10}, self.expected(
            {'name': 'quantity', 'path': 'quantity', 'equals': 10}))
        self.assertTrue(report['checks_passed'])
        self.assertEqual(report['execution']['model_execution'], 'not_evidenced')
        self.assertFalse(report['execution']['model_was_called_by_this_evaluator'])

    def test_missing_and_null_are_different(self):
        expected = self.expected({'name': 'must_be_null', 'path': 'quantity', 'equals': None})
        self.assertTrue(evaluate({'quantity': None}, expected)['checks_passed'])
        self.assertFalse(evaluate({}, expected)['checks_passed'])

    def test_positive_integer_does_not_accept_boolean(self):
        self.assertFalse(equal(True, 1))
        self.assertFalse(equal(True, 1, numeric=True))

    def test_numeric_unit_normalization_uses_decimal_and_rejects_nan(self):
        self.assertTrue(equal('3.1750', '3.175', numeric=True))
        self.assertFalse(equal('3.0', '3.175', numeric=True))
        self.assertFalse(equal('NaN', 'NaN', numeric=True))

    def test_raw_rates_and_customer_quote_words_are_allowed(self):
        report = evaluate({'inquiry': '请准备报价', 'material_rate': '20.00',
                           'operation_rates': [{'operation': '切割', 'value': '12', 'unit': 'CNY/piece'}],
                           'setup_fee': '200.00'}, self.expected())
        self.assertTrue(report['checks_passed'])

    def test_derived_quote_output_is_a_scope_violation(self):
        report = evaluate({'result': {'quote_total': '816.00'}}, self.expected())
        self.assertFalse(report['checks_passed'])
        self.assertEqual(report['results'][-1]['structured_output_violations'], ['result.quote_total'])

    def test_declared_model_without_complete_evidence_is_not_evidenced(self):
        report = evaluate({}, self.expected(), {'run_kind': 'dsh_model',
                          'model_execution': {'model': 'unknown'}})
        self.assertEqual(report['execution']['model_execution'], 'not_evidenced')

    def test_run_metadata_is_described_as_not_independently_verified(self):
        evidence = {'session_id': 'recorded-session', 'model': 'configured-model',
                    'started_at': '2026-10-18T00:00:00Z',
                    'completed_at': '2026-10-18T00:01:00Z', 'log_sha256': 'a' * 64}
        report = evaluate({}, self.expected(), {'run_kind': 'dsh_model', 'model_execution': evidence})
        self.assertIn('not_independently_verified', report['execution']['model_execution'])


if __name__ == '__main__':
    unittest.main()
