import json
import unittest
from pathlib import Path


class SampleFixtureTests(unittest.TestCase):
    def test_synthetic_samples_have_unique_ids_and_boolean_labels(self):
        path = Path(__file__).parent / "fixtures" / "moderation_samples.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(data["rule"])
        samples = data["samples"]
        self.assertEqual(len({sample["id"] for sample in samples}), len(samples))
        self.assertEqual({sample["category"] for sample in samples}, {"positive", "negative", "injection"})
        self.assertTrue(all(type(sample["violation"]) is bool for sample in samples))
