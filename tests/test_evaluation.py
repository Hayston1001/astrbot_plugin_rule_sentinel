import unittest

from sentinel.evaluation import compare_group_audits, summarize, summarize_group_audit


class EvaluationTests(unittest.TestCase):
    def test_error_rates_exclude_unknowns(self):
        result = summarize([
            {"violation": True, "status": "proposed", "latency_ms": 10, "cost": 0.01},
            {"violation": True, "status": "no_action", "latency_ms": 20},
            {"violation": False, "status": "proposed"},
            {"violation": False, "status": "no_action"},
            {"violation": True, "status": "timed_out"},
            {"violation": False, "status": "review"},
        ])
        self.assertEqual(result["false_positive_rate"], 0.5)
        self.assertEqual(result["false_negative_rate"], 0.5)
        self.assertEqual(result["unprocessed"], 2)
        self.assertEqual(result["status_counts"]["timed_out"], 1)
        self.assertEqual(result["status_counts"]["review"], 1)
        self.assertEqual(result["latency_p95_ms"], 20)
        self.assertEqual(result["cost_count"], 1)

    def test_invalid_and_empty(self):
        self.assertIsNone(summarize([])["false_positive_rate"])
        with self.assertRaises(ValueError):
            summarize([{"violation": True, "status": "unknown"}])
        with self.assertRaises(ValueError):
            summarize([{"violation": True, "status": "failed", "latency_ms": float("nan")}])
        with self.assertRaises(ValueError):
            summarize([{"violation": True, "status": []}])
        with self.assertRaises(ValueError):
            summarize([{"violation": True, "status": "failed", "latency_ms": 10 ** 400}])
        with self.assertRaises(ValueError):
            summarize([{"violation": True, "status": "failed", "cost": 1e308}] * 2)

    def test_invalid_audit_latency_is_not_reported(self):
        result = summarize_group_audit([{"message_id": "a", "violation": False}],
            [{"id": 1, "message_id": "a", "status": "no_action",
              "detail": '{"model_latency_ms":' + '9' * 400 + '}'}])
        self.assertEqual(result["latency_count"], 0)

    def test_status_counts_separate_unprocessed_reasons(self):
        labels = [{"message_id": str(i), "violation": False} for i in range(7)]
        statuses = ["review", "rejected", "failed", "timed_out", "skipped", "started"]
        events = [{"id": i + 1, "message_id": str(i), "status": status}
                  for i, status in enumerate(statuses)]
        result = summarize_group_audit(labels, events)
        for status in (*statuses, "missing"):
            self.assertEqual(result["status_counts"][status], 1)
        self.assertEqual(result["unprocessed"], 7)
        self.assertIsNone(result["false_positive_rate"])

    def test_latest_attempt_and_missing_are_not_counted_as_normal(self):
        labels = [{"message_id": "a", "violation": True}, {"message_id": "b", "violation": False}]
        events = [{"id": 1, "message_id": "a", "status": "proposed", "detail": "{}"},
                  {"id": 2, "message_id": "a", "status": "started", "detail": "{}"}]
        result = summarize_group_audit(labels, events)
        self.assertEqual(result["unprocessed"], 2)
        self.assertEqual(result["missing_audit"], 1)
        self.assertEqual(result["status_counts"]["started"], 1)
        self.assertEqual(result["status_counts"]["missing"], 1)
        self.assertEqual(result["status_counts"]["skipped"], 0)
        self.assertIsNone(result["false_negative_rate"])

    def test_duplicate_labels_and_audit_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            summarize_group_audit([{"message_id": "a", "violation": True}, {"message_id": "a", "violation": False}], [])
        with self.assertRaises(ValueError):
            summarize_group_audit([], [{"id": 1, "message_id": "a", "status": "started"},
                                       {"id": 1, "message_id": "b", "status": "started"}])
        with self.assertRaises(ValueError):
            summarize_group_audit([], [{"id": 1, "message_id": "a", "status": "missing"}])

    def test_usage_counts_only_reported_values(self):
        labels = [{"message_id": "a", "violation": True}, {"message_id": "b", "violation": False}]
        events = [{"id": 1, "message_id": "a", "status": "proposed", "detail": '{"usage":{"input_other":4,"input_cached":3,"output":2}}'},
                  {"id": 2, "message_id": "b", "status": "no_action", "detail": '{"usage":{"output_tokens":5}}'}]
        result = summarize_group_audit(labels, events)
        self.assertEqual(result["usage"]["input_tokens"], {"reported": 1, "total": 7})
        self.assertEqual(result["usage"]["output_tokens"], {"reported": 2, "total": 7})

    def test_comparison_uses_only_samples_both_models_finished(self):
        labels = [{"message_id": "a", "violation": True}, {"message_id": "b", "violation": False}]
        first = [{"id": 1, "message_id": "a", "status": "proposed"},
                 {"id": 2, "message_id": "b", "status": "proposed"}]
        second = [{"id": 1, "message_id": "a", "status": "no_action"},
                  {"id": 2, "message_id": "b", "status": "review"}]
        result = compare_group_audits(labels, first, second)
        self.assertEqual((result["paired_completed"], result["not_paired"], result["paired_disagreements"]), (1, 1, 1))
        self.assertEqual(result["first_paired"]["true_positive"], 1)
        self.assertEqual(result["second_paired"]["false_negative"], 1)
        self.assertEqual(result["second_coverage"]["unprocessed"], 1)
