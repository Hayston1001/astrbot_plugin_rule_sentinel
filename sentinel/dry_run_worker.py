"""Async dry-run queue worker. No platform actions or chat replies are exposed."""

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from .dry_run_queue import DryRunQueue, WorkItem, AUDIT_DETAIL_VERSION, MAX_AUDIT_DETAIL_CHARS
from .decision_errors import AuditSnapshotTooLarge, AuditUnavailable, ContextLimitExceeded, ProviderIntegrityUnverified
from .async_io import run_awaitable_safely, run_sync_safely


def _safe_error_fields(error):
    """Keep bounded provider/error metadata without exception text or headers."""
    result = {"type": type(error).__name__}
    for key in ("status_code", "status", "code", "error_code", "http_status", "retry_after"):
        try:
            value = getattr(error, key, None)
        except Exception:
            continue
        if type(value) is int or (isinstance(value, str) and value.strip()):
            text = str(value)
            if len(text) <= 64:
                result[key] = value if type(value) is int else text
    try:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    except Exception:
        status = None
    if type(status) is int and "http_status" not in result:
        result["http_status"] = status
    cause = getattr(error, "__cause__", None)
    if cause is not None:
        result["cause"] = type(cause).__name__
    return result


def _failure_detail(reason_code, *, error=None, stage="decision", **fields):
    detail = {"reason": reason_code, "reason_code": reason_code, "stage": stage,
              "detail_version": AUDIT_DETAIL_VERSION, **fields}
    if error is not None:
        detail["error"] = _safe_error_fields(error)
    return json.dumps(detail, ensure_ascii=False, allow_nan=False)


def _log_failure(*, reason_code, error, attempt_id):
    """Write one sanitized correlation line without blocking the decision path."""
    try:
        from astrbot.api import logger
        fields = [f"code={reason_code}", f"error={type(error).__name__}", f"attempt={attempt_id}"]
        for key in ("status_code", "code", "error_code", "http_status"):
            try:
                value = getattr(error, key, None)
            except Exception:
                continue
            if type(value) is int or (isinstance(value, str) and len(value) <= 64):
                fields.append(f"{key}={value}")
        logger.warning("Rule Sentinel decision failed: " + " ".join(fields))
    except Exception:
        pass


@dataclass(frozen=True)
class Decision:
    status: str
    detail: str
    model_attempted: bool = True
    rule_versions: tuple[tuple[str, int], ...] | None = None
    settings_fingerprint: str | None = None


class DryRunWorker:
    """Drain groups using a shared concurrency limit and a bounded decision time.

    The caller schedules groups and must not call exclusive recovery while any
    worker is alive. The decider is restricted to dry-run work by its caller;
    this class deliberately has no action executor. Cancellation leaves the
    item leased for recovery, rather than claiming a result was recorded.
    """

    def __init__(self, queue: DryRunQueue, decide: Callable[[WorkItem], Awaitable[Decision]],
                 *, concurrency: int, timeout_seconds: float, breaker=None, notify=None):
        if type(concurrency) is not int or concurrency <= 0:
            raise ValueError("invalid concurrency")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds < float("inf"):
            raise ValueError("invalid timeout")
        self.queue = queue
        self.decide = decide
        self.timeout = timeout_seconds
        self.limit = asyncio.Semaphore(concurrency)
        self.breaker = breaker
        self.notify = notify
        self.stopping = False
        self.on_finished = None

    def request_stop(self):
        self.stopping = True

    def _notify(self, code):
        if self.notify is not None:
            try:
                self.notify(code)
            except Exception:
                pass

    async def drain_group(self, platform_id: str, group_id: str) -> int:
        finished = 0
        while not self.stopping:
            async with self.limit:
                if self.stopping:
                    return finished
                try:
                    item = await run_sync_safely(self.queue.claim, platform_id, group_id)
                except Exception:
                    self._notify("storage_failed")
                    raise
                if item is None:
                    return finished
                if self.stopping:
                    if not await run_sync_safely(self.queue.return_unstarted, item):
                        raise RuntimeError("queue lease lost while stopping")
                    return finished
                token = await self.breaker.acquire() if self.breaker else None
                if self.stopping:
                    if self.breaker and token is not None:
                        await run_awaitable_safely(self.breaker.abandon(token))
                    if not await run_sync_safely(self.queue.return_unstarted, item):
                        raise RuntimeError("queue lease lost while stopping")
                    return finished
                if self.breaker and token is None:
                    result = Decision("skipped", "circuit_open")
                else:
                    try:
                        result = await asyncio.wait_for(self.decide(item), timeout=self.timeout)
                        if not isinstance(result, Decision):
                            result = Decision("failed", _failure_detail(
                                "invalid_decider_result", received_type=type(result).__name__))
                        elif result.status not in {"proposed", "no_action", "rejected", "failed", "timed_out", "skipped", "review"}:
                            result = Decision("failed", _failure_detail(
                                "invalid_decider_status", received_status=str(result.status)[:64]))
                    except TimeoutError:
                        result = Decision("timed_out", "decision_timeout")
                    except ContextLimitExceeded:
                        result = Decision("failed", "context_length_exceeded")
                    except AuditUnavailable:
                        result = Decision("failed", "evaluation_audit_unavailable", model_attempted=False)
                    except AuditSnapshotTooLarge:
                        result = Decision("failed", "rule_snapshot_too_large", model_attempted=False)
                    except ProviderIntegrityUnverified:
                        result = Decision("skipped", "provider_integrity_unverified", model_attempted=False)
                    except Exception as error:
                        # Exception text may contain group messages or credentials.
                        result = Decision("failed", _failure_detail("decision_error", error=error))
                        _log_failure(reason_code="decision_error", error=error, attempt_id=item.lease)
                    except BaseException:
                        if self.breaker:
                            await run_awaitable_safely(self.breaker.complete(token, success=False))
                        raise
                    if self.breaker:
                        if result.model_attempted:
                            await run_awaitable_safely(self.breaker.complete(token, success=result.status not in ("failed", "timed_out")))
                        else:
                            await run_awaitable_safely(self.breaker.abandon(token))
                if not isinstance(result.detail, str) or not result.detail.strip() or len(result.detail) > MAX_AUDIT_DETAIL_CHARS:
                    # Do not silently truncate rule snapshots or leave an item
                    # permanently leased because its audit cannot be stored.
                    result = Decision("failed", _failure_detail(
                        "audit_detail_invalid_or_too_large", detail_truncated=True))
                try:
                    finish_args = {"status": result.status, "detail": result.detail}
                    if result.rule_versions is not None and result.status in ("proposed", "no_action"):
                        finish_args["rule_versions"] = result.rule_versions
                    if result.settings_fingerprint is not None and result.status in ("proposed", "no_action"):
                        finish_args["settings_fingerprint"] = result.settings_fingerprint
                    recorded = await run_sync_safely(self.queue.finish, item, **finish_args)
                except Exception:
                    self._notify("storage_failed")
                    raise
                if not recorded:
                    self._notify("storage_failed")
                    raise RuntimeError("queue lease lost before audit")
                if self.on_finished is not None:
                    await self.on_finished(item)
                if result.detail == "context_length_exceeded":
                    self._notify("context_limit")
                elif result.detail == "rule_snapshot_too_large":
                    self._notify("rule_snapshot_too_large")
                elif result.detail == "evaluation_audit_unavailable":
                    self._notify("storage_failed")
                elif result.status == "timed_out":
                    self._notify("model_timeout")
                elif result.detail == "circuit_open":
                    self._notify("circuit_open")
                elif result.status == "failed":
                    self._notify("model_failed")
                finished += 1
        return finished
