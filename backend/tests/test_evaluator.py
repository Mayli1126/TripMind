from pathlib import Path
from unittest import TestCase

from evaluation.evaluator import evaluate_records, load_jsonl


class OfflineEvaluatorTests(TestCase):
    def test_sample_dataset_produces_metrics(self):
        dataset = Path(__file__).parents[1] / "evaluation" / "sample_cases.jsonl"
        result = evaluate_records(load_jsonl(dataset))

        self.assertEqual(result["total_cases"], 1)
        self.assertEqual(result["schema_valid_rate"], 1)
        self.assertEqual(result["executable_day_rate"], 1)
        self.assertEqual(result["budget_drift_rate"], 1)
        self.assertGreater(result["average_route_saved_percent"], 0)

