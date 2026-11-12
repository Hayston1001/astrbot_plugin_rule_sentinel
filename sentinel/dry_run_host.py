"""Opt-in dry-run lifecycle host, not attached to AstrBot events by default."""

import asyncio
import logging

from .async_io import run_sync_safely
from .event_intake import EventIntake
from .instance_lock import InstanceLock
from .notifications import build_notification_dispatcher
from .plugin_settings import read_settings
from .runtime_factory import build_runtime


class LockInUseError(RuntimeError):
    """The dry-run queue is already owned by another host."""


class DryRunHost:
    """Own queue lock, model resources and workers until graceful shutdown.

    Construction and start do not install an event hook. A caller must first
    establish that its event source is safe and authorized. Real mute actions
    are available only when explicitly enabled.
    """

    def __init__(self, *, context, config, rules, queue, lock_path, session_factory=None, session_gate=None,
                 on_failure=None):
        self.context, self.config, self.rules, self.queue = context, config, rules, queue
        self.lock = InstanceLock(lock_path)
        self.session_factory = session_factory
        self.session_gate = session_gate
        self.on_failure = on_failure
        self.session = None
        self.mute_session = None
        self.mute_executor = None
        self._mute_tasks = set()
        self._plan_tasks = {}
        self.runtime = None
        self.intake = None
        self.notifications = None
        self.started = False
        self.closed = False
        self._starting = False
        self._close_task = None
        self._startup_done = asyncio.Event()
        self._startup_done.set()

    def _report_failure(self, code):
        logging.getLogger(__name__).error("Rule Sentinel background failure: %s", code)
        if self.on_failure is not None:
            try:
                self.on_failure(code)
            except Exception:
                pass

    async def start(self):
        if self.started or self.closed or self._starting:
            raise RuntimeError("host cannot start again")
        self._starting = True
        self._startup_done.clear()
        try:
            await self._start()
        finally:
            self._starting = False
            self._startup_done.set()

    async def _start(self):
        settings = read_settings(self.config)
        # The message listener runs after the framework's whitelist, session and
        # plugin checks, so a separate gate is optional. A host fed by a source
        # outside that pipeline must still pass one explicitly.
        target = self.config.get("notification_target", "")
        interval = self.config.get("notification_interval_seconds", 0)
        if not isinstance(target, str) or type(interval) is not int or interval < 0:
            raise ValueError("invalid notification settings")
        if bool(target.strip()) != (interval > 0):
            raise ValueError("notification target and positive interval must be configured together")
        acquire = asyncio.create_task(asyncio.to_thread(self.lock.acquire))
        try:
            acquired = await asyncio.shield(acquire)
        except asyncio.CancelledError:
            # A cancelled await does not stop its SQLite/OS thread. Wait for
            # lock acquisition to finish and release it if it succeeded.
            while not acquire.done():
                try:
                    await asyncio.shield(acquire)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not acquire.cancelled() and acquire.exception() is None and acquire.result():
                await run_sync_safely(self.lock.release)
            raise
        if not acquired:
            raise LockInUseError("dry-run queue already in use")
        if self.closed:
            await run_sync_safely(self.lock.release)
            raise RuntimeError("host closed during startup")
        try:
            from .mute_execution_store import MuteExecutionStore
            store = MuteExecutionStore(self.queue.path, notify=self.queue.publish_intent)
            await run_sync_safely(store.recover_interrupted)
            if self.config.get("action_execution_enabled") is True:
                import aiohttp
                from .mute_executor import MuteExecutor
                from .qq_mute_platform import QQMutePlatform, resolve_qq_credentials
                self.mute_session = aiohttp.ClientSession(trust_env=False)
                self.mute_executor = MuteExecutor(store, QQMutePlatform(self.mute_session,
                    lambda platform_id: resolve_qq_credentials(self.context, platform_id)))
            overrides = self.config.get("group_overrides", {})
            jev_needed = settings.backend == "jev" or (isinstance(overrides, dict) and any(
                isinstance(item, dict) and isinstance(item.get("model"), dict) and item["model"].get("backend") == "jev"
                for item in overrides.values()))
            if jev_needed:
                if self.session_factory is None:
                    import aiohttp
                    self.session = aiohttp.ClientSession()
                else:
                    self.session = self.session_factory()
            if interval > 0:
                self.notifications = build_notification_dispatcher(context=self.context, queue=self.queue,
                    recipient=target, interval_seconds=interval,
                    is_authorized=lambda: (self.config.get("notification_target", "") == target
                        and self.config.get("notification_interval_seconds", 0) == interval), on_failure=self._report_failure)
                self.notifications.start()
            self.runtime = build_runtime(context=self.context, config=self.config, rules=self.rules,
                                         queue=self.queue, http_session=self.session,
                                         notify=self.notifications.offer if self.notifications else None,
                                          session_gate=self.session_gate, on_failure=self._report_failure)
            if self.mute_executor is not None:
                self.runtime.worker.on_finished = self._schedule_item_mute
            await self.runtime.start_after_exclusive_stop()
            if self.closed:
                raise RuntimeError("host closed during startup")
            self.intake = EventIntake(self.runtime, self.queue, capacity=settings.ingress_capacity,
                                      on_failure=self._report_failure)
            self.intake.start()
            self.started = True
            if self.mute_executor is not None:
                for intent_id in await run_sync_safely(self.mute_executor.store.pending_auto):
                    await self._schedule_plan(intent_id)
        except BaseException:
            # No more resources will be created on this path. Allow a close
            # already waiting for startup to finish before awaiting it here.
            self._startup_done.set()
            await self.close()
            raise

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)

    def _start_mute(self, intent_id, mode):
        if self.closed or self.mute_executor is None:
            raise RuntimeError("mute executor unavailable")
        task = asyncio.create_task(self.mute_executor.execute(intent_id, mode=mode))
        self._mute_tasks.add(task)
        def done(completed):
            self._mute_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                self._report_failure("mute_execution_failed")
        task.add_done_callback(done)
        return task

    async def _schedule_item_mute(self, item):
        intent_id = await run_sync_safely(self.queue.pending_auto_intent, item)
        if intent_id is not None and not self.closed:
            await self._schedule_plan(intent_id)

    async def _schedule_plan(self, intent_id):
        plan_id = await run_sync_safely(self.mute_executor.store.plan_for_intent, intent_id)
        task = self._plan_tasks.get(plan_id)
        if task is None or task.done():
            async def run_plan():
                for action_id in await run_sync_safely(self.mute_executor.store.pending_plan_auto, plan_id):
                    await self.mute_executor.execute(action_id, mode="auto")
            task = asyncio.create_task(run_plan())
            self._plan_tasks[plan_id] = task
            self._mute_tasks.add(task)
            def done(completed):
                self._mute_tasks.discard(completed)
                if self._plan_tasks.get(plan_id) is completed:
                    self._plan_tasks.pop(plan_id, None)
                if not completed.cancelled() and completed.exception() is not None:
                    self._report_failure("action_execution_failed")
            task.add_done_callback(done)
        return task

    async def confirm_mute(self, intent_id):
        plan = await self._schedule_plan(intent_id)
        await asyncio.shield(plan)
        return await asyncio.shield(self._start_mute(intent_id, "confirm"))

    async def _close_resources(self):
        # Do not release the lock until the intake and runtime have drained.
        if self.runtime is not None:
            self.runtime.stop_new_decisions()
        # Startup may still be acquiring the lock or creating resources. Do
        # not close them or release ownership until it has stopped doing so.
        await self._startup_done.wait()
        if self.intake is not None:
            await self.intake.close()
        if self.runtime is not None:
            await self.runtime.close()
        if self._mute_tasks:
            await asyncio.gather(*list(self._mute_tasks), return_exceptions=True)
        if self.notifications is not None:
            await self.notifications.close()
            if self.notifications.audit_failures:
                self._report_failure("notification_audit_failed")
            if self.notifications.dropped:
                self._report_failure("notification_buffer_full")
        if self.session is not None:
            await self.session.close()
        if self.mute_session is not None:
            await self.mute_session.close()
        await run_sync_safely(self.lock.release)
