"""Offline-only reporter selection/denominator/accounting regressions."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import combine_calibration_runs as combine


class CombineTests(unittest.TestCase):
    def sources(self):
        base = {'model': 'mimo-v2.6-flash', 'endpoint': 'https://example.invalid/v1/chat/completions'}
        old = {'spec': {**base, 'executor_version': 'scenario-executor-1.1.1', 'inputs': {'S001': 'a', 'S039': 'b'}},
               'accepted': {'S001': {'any_label': 'wrong'}}, 'evaluation_failed_samples': ['S039']}
        new = {'spec': {**base, 'executor_version': 'scenario-executor-1.1.2', 'inputs': {'S039': 'b'}},
               'accepted': {'S039': {'any_label': 'different_from_reference'}}, 'evaluation_failed_samples': []}
        return old, new

    def test_only_oldfailed_to_newvalid_selection_not_best_of_labels(self):
        old, new = self.sources()
        self.assertEqual(combine.choose_sources(old, new, ['S001', 'S039']), {'S001': 'original', 'S039': 'recovery'})

    def test_refuse_replacing_original_success_or_incomplete_recovery(self):
        for case in ('old_success', 'new_failed', 'extra_new', 'endpoint_different'):
            old, new = self.sources()
            if case == 'old_success': old['accepted']['S039'] = {}
            elif case == 'new_failed': new['accepted'] = {}; new['evaluation_failed_samples'] = ['S039']
            elif case == 'extra_new': new['spec']['inputs']['S001'] = 'a'
            else: new['spec']['endpoint'] = 'https://changed.invalid/v1/chat/completions'
            with self.subTest(case=case), self.assertRaises(AssertionError):
                combine.choose_sources(old, new, ['S001', 'S039'])

    def test_empty_candidates_excluded_from_candidate_sample_denominators(self):
        row = {'baseline_id': 'AUC-001', 'coverage': 'missing', 'business_consistency': 'not_assessed'}
        result = {'baseline_judgments': [row], 'generated_judgments': [], 'duplicate_groups': [], 'review_queue': []}
        records = {'EMPTY': {'task_scope': {'category_ids': ['AUC']}, 'baseline': [{'id': 'AUC-001', 'category': 'AUC'}], 'generated_scenarios': []}}
        report = combine.compare_labels({'EMPTY': result}, {'EMPTY': copy.deepcopy(result)}, records)
        self.assertEqual(report['coverage']['rows_compared'], 1)
        self.assertEqual(report['coverage']['samples_compared'], 1)
        self.assertEqual(report['label_agreement']['generated_judgments.relation']['samples_compared'], 0)
        self.assertEqual(set(report['six_category_denominators']), set(combine.CATEGORIES))
        self.assertEqual(report['six_category_denominators']['AMT']['baseline_row_denominator'], 0)

    def test_usage_counts_all_attempts_not_only_selected_final(self):
        http = [{'usage': {'total_tokens': 12}}, {'usage': None}, {'usage': {'total_tokens': 7, 'prompt_tokens': 3, 'debug': True}}]
        self.assertEqual(combine.usage_from_http(http), {'prompt_tokens': 3, 'total_tokens': 19})

    def test_existing_output_rejected_before_reading_sources(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(combine, 'inspect_run', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ValueError, 'output_exists'):
                combine.combine('old', 'new', temp, 'dataset')


if __name__ == '__main__': unittest.main()
