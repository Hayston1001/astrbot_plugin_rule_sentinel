import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from scripts.evaluate_dry_run import load_labels, main


class EvaluationCliTests(unittest.TestCase):
    def test_scoped_aggregate_does_not_print_message_or_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, labels = root / "db.sqlite", root / "labels.json"
            queue = DryRunQueue(path)
            queue.initialize()
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute("INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail) VALUES (?, ?, ?, ?, ?)",
                           ("p", "g", "one", "proposed", '{"reason":"private","model_latency_ms":12}'))
                db.execute("INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail) VALUES (?, ?, ?, ?, ?)",
                           ("p", "other", "two", "proposed", "{}"))
                db.commit()
            labels.write_text(json.dumps([{"message_id": "one", "violation": True},
                                          {"message_id": "two", "violation": False}]), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["--database", str(path), "--platform", "p", "--group", "g", "--labels", str(labels)])
            aggregate = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(aggregate["true_positive"], 1)
            self.assertEqual(aggregate["missing_audit"], 1)
            self.assertNotIn("private", output.getvalue())

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels = root / "labels.json"
            labels.write_text("[]", encoding="utf-8")
            database = root / "missing.sqlite"
            with contextlib.redirect_stderr(io.StringIO()):
                result = main(["--database", str(database), "--platform", "p", "--group", "g", "--labels", str(labels)])
            self.assertEqual(result, 2)
            self.assertFalse(database.exists())

    def test_duplicate_fields_and_deep_labels_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "labels.json"
            for data in ('[{"message_id":"a","violation":true,"violation":false}]',
                         '[{"message_id":"a","message_id":"b","violation":true}]',
                         '[' * 1100 + '0' + ']' * 1100,
                         '[{"message_id":"a","violation":NaN}]'):
                path.write_text(data, encoding="utf-8")
                with self.subTest(data=data[:80]), self.assertRaises(ValueError):
                    load_labels(path)

    def test_label_read_enforces_actual_size_limit(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "labels.json"
            path.write_text('[]   ', encoding="utf-8")
            with patch("scripts.evaluate_dry_run.MAX_LABEL_BYTES", 4):
                with self.assertRaises(ValueError):
                    load_labels(path)
