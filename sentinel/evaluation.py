"""Offline aggregation of human-labeled dry-run outcomes.

Unprocessed and review outcomes are excluded from measured error rates and
reported separately; they must not be counted as correct or normal messages.
"""

import math
import json


AUDIT_STATUSES = ("proposed", "no_action", "review", "rejected", "failed", "timed_out", "skipped", "started")
SUMMARY_STATUSES = (*AUDIT_STATUSES, "missing")


def _finite_nonnegative(value):
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def summarize(samples: list[dict]) -> dict:
    if not isinstance(samples, list):
        raise ValueError("samples must be a list")
    counts = {"true_positive": 0, "false_positive": 0, "true_negative": 0,
              "false_negative": 0, "unprocessed": 0,
              "unprocessed_positive": 0, "unprocessed_negative": 0}
    latencies, costs = [], []
    usage_counts = {"input_tokens": {"reported": 0, "total": 0},
                    "output_tokens": {"reported": 0, "total": 0}}
    status_counts = {status: 0 for status in SUMMARY_STATUSES}
    for sample in samples:
        if (not isinstance(sample, dict) or type(sample.get("violation")) is not bool
                or not isinstance(sample.get("status"), str) or sample["status"] not in status_counts):
            raise ValueError("invalid labeled sample")
        status, expected = sample["status"], sample["violation"]
        status_counts[status] += 1
        if status == "proposed":
            counts["true_positive" if expected else "false_positive"] += 1
        elif status == "no_action":
            counts["false_negative" if expected else "true_negative"] += 1
        else:
            counts["unprocessed"] += 1
            counts["unprocessed_positive" if expected else "unprocessed_negative"] += 1
        for key, target in (("latency_ms", latencies), ("cost", costs)):
            value = sample.get(key)
            if value is not None:
                if not _finite_nonnegative(value):
                    raise ValueError("invalid metric")
                target.append(value)
        usage = sample.get("usage")
        if usage is not None:
            if not isinstance(usage, dict) or set(usage) - set(usage_counts):
                raise ValueError("invalid token usage")
            for key, value in usage.items():
                if type(value) is not int or value < 0:
                    raise ValueError("invalid token usage")
                usage_counts[key]["reported"] += 1
                usage_counts[key]["total"] += value
    fp, tn = counts["false_positive"], counts["true_negative"]
    fn, tp = counts["false_negative"], counts["true_positive"]
    latencies.sort()
    try:
        total_cost = math.fsum(costs) if costs else None
    except OverflowError:
        raise ValueError("total cost exceeds numeric range") from None
    return {"total": len(samples), **counts, "status_counts": status_counts,
            "false_positive_rate": fp / (fp + tn) if fp + tn else None,
            "false_negative_rate": fn / (fn + tp) if fn + tp else None,
            "latency_count": len(latencies),
            "latency_p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
            "cost_count": len(costs), "known_total_cost": total_cost,
            "usage": usage_counts}


def summarize_group_audit(labels: list[dict], events: list[dict]) -> dict:
    """Join one group's human labels to the latest event for each message.

    A newer started event supersedes an earlier completed attempt. Missing
    events are unprocessed, not assumed normal. Labels and events must already
    be scoped to the same authenticated platform/group by the caller.
    """
    if not isinstance(labels, list) or not isinstance(events, list):
        raise ValueError("labels and events must be lists")
    latest = {}
    seen_ids = set()
    for event in events:
        if not isinstance(event, dict) or type(event.get("id")) is not int or event["id"] <= 0:
            raise ValueError("invalid audit event")
        if event["id"] in seen_ids:
            raise ValueError("duplicate audit event id")
        seen_ids.add(event["id"])
        message_id, status = event.get("message_id"), event.get("status")
        if not isinstance(message_id, str) or not message_id.strip() or status not in AUDIT_STATUSES:
            raise ValueError("invalid audit event")
        if message_id not in latest or event["id"] > latest[message_id]["id"]:
            latest[message_id] = event
    samples, seen_messages = [], set()
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("message_id"), str) or not label["message_id"].strip() or type(label.get("violation")) is not bool:
            raise ValueError("invalid label")
        if label["message_id"] in seen_messages:
            raise ValueError("duplicate message label")
        seen_messages.add(label["message_id"])
        event = latest.get(label["message_id"])
        sample = {"violation": label["violation"], "status": event["status"] if event else "missing"}
        if event and isinstance(event.get("detail"), str):
            try:
                detail = json.loads(event["detail"])
            except (ValueError, TypeError):
                detail = None
            if isinstance(detail, dict):
                if isinstance(detail.get("decision"), dict):
                    detail = detail["decision"]
                latency = detail.get("model_latency_ms")
                if _finite_nonnegative(latency):
                    sample["latency_ms"] = latency
                usage = detail.get("usage")
                if isinstance(usage, dict):
                    normalized = {}
                    direct_input = usage.get("input_tokens")
                    input_other, input_cached = usage.get("input_other"), usage.get("input_cached")
                    if type(direct_input) is int and direct_input >= 0:
                        normalized["input_tokens"] = direct_input
                    elif type(input_other) is int and input_other >= 0 and type(input_cached) is int and input_cached >= 0:
                        normalized["input_tokens"] = input_other + input_cached
                    output = usage.get("output_tokens", usage.get("output"))
                    if type(output) is int and output >= 0:
                        normalized["output_tokens"] = output
                    if normalized:
                        sample["usage"] = normalized
        samples.append(sample)
    result = summarize(samples)
    result["missing_audit"] = sum(label["message_id"] not in latest for label in labels)
    return result


def compare_group_audits(labels: list[dict], first_events: list[dict], second_events: list[dict]) -> dict:
    """Compare two runs only on human-labeled messages completed by both.

    Both event sets must already be scoped to their respective test group and
    use matching message IDs. Separate coverage is reported so dropped,
    reviewed and failed samples cannot improve a model's apparent accuracy.
    """
    first_all = summarize_group_audit(labels, first_events)
    second_all = summarize_group_audit(labels, second_events)

    def latest(events):
        result = {}
        for event in events:
            message_id = event["message_id"]
            if message_id not in result or event["id"] > result[message_id]["id"]:
                result[message_id] = event
        return result

    first, second = latest(first_events), latest(second_events)
    first_pairs, second_pairs = [], []
    disagreements = 0
    for label in labels:
        first_event, second_event = first.get(label["message_id"]), second.get(label["message_id"])
        if (not first_event or not second_event or first_event["status"] not in ("proposed", "no_action")
                or second_event["status"] not in ("proposed", "no_action")):
            continue
        first_pairs.append({"violation": label["violation"], "status": first_event["status"]})
        second_pairs.append({"violation": label["violation"], "status": second_event["status"]})
        disagreements += first_event["status"] != second_event["status"]
    return {"total_labels": len(labels), "paired_completed": len(first_pairs),
            "not_paired": len(labels) - len(first_pairs), "paired_disagreements": disagreements,
            "first_coverage": first_all, "second_coverage": second_all,
            "first_paired": summarize(first_pairs), "second_paired": summarize(second_pairs)}
