"""Async dry-run queue worker. No platform actions or chat replies are exposed."""

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from .dry_run_queue import DryRunQueue, WorkItem, MAX_AUDIT_DETAIL_CHARS
from .decision_errors import AuditSnapshotTooLarge, AuditUnavailable, ContextLimitExceeded, ProviderIntegrityUnverified
from .async_io import run_awaitable_safely, run_sync_safely


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
                            result = Decision("failed", "invalid_decider_result")
                        elif result.status not in {"proposed", "no_action", "rejected", "failed", "timed_out", "skipped", "review"}:
                            result = Decision("failed", "invalid_decider_status")
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
                    except Exception:
                        # Error messages may contain group messages or credentials.
                        result = Decision("failed", "decision_error")
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
                    result = Decision("failed", "audit_detail_invalid_or_too_large")
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
