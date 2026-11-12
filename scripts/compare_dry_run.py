"""Read-only paired comparison of two separately recorded dry-run audits."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sentinel.evaluation import compare_group_audits
from scripts.evaluate_dry_run import load_labels, read_audit


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare two dry-run datasets on the same labeled message IDs")
    parser.add_argument("--first-database", required=True, type=Path)
    parser.add_argument("--second-database", required=True, type=Path)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--labels", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        labels = load_labels(args.labels)
        first = read_audit(args.first_database, args.platform, args.group, labels)
        second = read_audit(args.second_database, args.platform, args.group, labels)
        print(json.dumps(compare_group_audits(labels, first, second), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, UnicodeError):
        print("Comparison failed: check databases, group and label format.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
