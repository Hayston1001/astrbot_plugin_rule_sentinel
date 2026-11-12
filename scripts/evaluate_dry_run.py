"""Read-only aggregate evaluation for one group and explicit human labels.

Input JSON is a list of {"message_id": "...", "violation": true|false}.
No message text is read from the queue or printed in the result.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sentinel.evaluation import summarize_group_audit


MAX_LABELS = 10000
MAX_EVENTS = 50000
MAX_LABEL_BYTES = 4 * 1024 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate label field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("invalid JSON constant")


def load_labels(path: Path) -> list[dict]:
    # Bound the actual read, not only a prior stat that can become stale.
    with path.open("rb") as file:
        raw = file.read(MAX_LABEL_BYTES + 1)
    if len(raw) > MAX_LABEL_BYTES:
        raise ValueError("label file exceeds size limit")
    try:
        labels = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except RecursionError:
        raise ValueError("label JSON is too deeply nested") from None
    if not isinstance(labels, list) or len(labels) > MAX_LABELS:
        raise ValueError("invalid label count")
    for label in labels:
        if (not isinstance(label, dict) or set(label) != {"message_id", "violation"}
                or not isinstance(label["message_id"], str) or not label["message_id"].strip()
                or type(label["violation"]) is not bool):
            raise ValueError("invalid label")
    return labels


def read_audit(path: Path, platform_id: str, group_id: str, labels: list[dict]) -> list[dict]:
    if any(not isinstance(value, str) or not value.strip() for value in (platform_id, group_id)):
        raise ValueError("platform and group are required")
    ids = [label["message_id"] for label in labels]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate message label")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        events = []
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            sql = ("SELECT id, message_id, status, detail FROM dry_run_events "
                   f"WHERE platform_id=? AND group_id=? AND message_id IN ({placeholders}) ORDER BY id")
            for row in db.execute(sql, (platform_id, group_id, *batch)):
                if len(events) >= MAX_EVENTS:
                    raise ValueError("audit event limit exceeded; split the labels into smaller batches")
                events.append(dict(row))
        return events
    finally:
        db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate dry-run results for one explicitly selected group")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--labels", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        labels = load_labels(args.labels)
        events = read_audit(args.database, args.platform, args.group, labels)
        print(json.dumps(summarize_group_audit(labels, events), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, UnicodeError):
        print("Evaluation failed: check the selected database, group and label format.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
