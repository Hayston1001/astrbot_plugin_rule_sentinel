import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from scripts.compare_dry_run import main


class ComparisonCliTests(unittest.TestCase):
    def test_pairwise_aggregate_is_private_and_excludes_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second, labels = root / "a.sqlite", root / "b.sqlite", root / "labels.json"
            for path, status in ((first, "proposed"), (second, "no_action")):
                DryRunQueue(path).initialize()
                with contextlib.closing(sqlite3.connect(path)) as db:
                    db.execute("INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail) VALUES ('p', 'g', 'one', ?, ?)",
                               (status, '{"reason":"private content"}'))
                    db.commit()
            labels.write_text(json.dumps([{"message_id": "one", "violation": True},
                                          {"message_id": "two", "violation": False}]), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(["--first-database", str(first), "--second-database", str(second),
                               "--platform", "p", "--group", "g", "--labels", str(labels)])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual((report["paired_completed"], report["not_paired"]), (1, 1))
            self.assertNotIn("private content", output.getvalue())
