"""Offline regression tests for native resources and comparison boundaries."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare_models as cm
import evaluate_scenarios as ev


class ComparisonTests(unittest.TestCase):
    def test_existing_output_refuses_before_authentication(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(cm, 'get_provider', side_effect=AssertionError('auth forbidden')):
            with self.assertRaisesRegex(ValueError, 'output_exists'):
                cm.compare('gpt', 'mimo', temp, 'protected', 'tests', [])

    def test_resources_sum_repairs_and_normalized_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            checked = {'run_path': temp, 'http_attempts': [
                {'status': 400, 'usage': None, 'duration_seconds': 1.0},
                {'status': 200, 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}, 'duration_seconds': 2.0}],
                'report': {'sample_status': [{'status': 'success', 'format_repairs': 1}]},
                'accepted': {'S001': {}}, 'evaluation_failed_samples': []}
            result = cm.resources(checked)
            self.assertEqual(result['http_count'], 2)
            self.assertEqual(result['usage_all_attempts']['total_tokens'], 30)
            self.assertEqual(result['http_duration_seconds_sum'], 3.0)
            self.assertEqual(result['first_format_success'], 0)
            self.assertEqual(result['repaired_success'], 1)
            self.assertIsNone(result['monetary_cost'])

    def test_subset_denominators_not_full_dataset(self):
        sample = ev.load(ev.ASSETS / 'calibration_samples.json')['samples'][0]
        actual = {sample['sample_id']: copy.deepcopy(sample['expected_judgments'])}
        records = {sample['sample_id']: sample['input']}
        result = cm.compare_labels(actual, actual, records)
        self.assertEqual(result['coverage']['samples_compared'], 1)
        self.assertEqual(result['coverage']['rows_compared'], len(sample['input']['baseline']))
        self.assertNotEqual(result['coverage']['samples_compared'], 40)
        self.assertEqual(set(result['six_category_denominators']), {'AUC', 'AMT', 'ACC', 'OPN', 'CLS', 'CAN'})


if __name__ == '__main__':
    unittest.main()
