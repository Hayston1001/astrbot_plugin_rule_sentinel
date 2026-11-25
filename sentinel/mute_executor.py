"""Injectable mute execution coordinator."""

import asyncio

from .async_io import run_sync_safely
from .notifications import notification_scope


class MuteExecutor:
    def __init__(self, store, platform, *, sleep=asyncio.sleep, notify=None):
        self.store, self.platform, self.sleep, self.notify = store, platform, sleep, notify

    def _notify_rejected(self, intent):
        if self.notify is not None:
            try:
                self.notify("action_rejected", notification_scope(
                    platform_id=intent["platform_id"], group_id=intent["group_id"],
                    message_id=intent["message_id"], rule_id=intent["rule_id"],
                    sender_id=intent["target_sender_id"],
                    action=intent["action"], duration_seconds=intent.get("duration_seconds")))
            except Exception:
                pass

    async def _acquire_guard(self, intent_id, attempt, *, notice=False, reply_options=None):
        acquire = self.store.acquire_notice_guard if notice else self.store.acquire_send_guard
        kwargs = {"expected_reply_options": reply_options} if reply_options is not None else {}
        task = asyncio.create_task(asyncio.to_thread(acquire, intent_id, attempt, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled() and task.exception() is None:
                guard = task.result()
                if guard is not None and not isinstance(guard, str):
                    await run_sync_safely(guard.close)
            raise

    async def execute(self, intent_id, *, mode, required_action=None):
        """Use durable claims and fixed expiry; unknown results never retry."""
        for attempt in range(1, 4):
            kwargs = {"requested_mode": mode, "allow_retry": attempt > 1}
            if required_action is not None:
                kwargs["required_action"] = required_action
            intent = await run_sync_safely(self.store.begin_attempt, intent_id, **kwargs)
            if intent is None:
                return "not_claimed"
            try:
                verify = (getattr(self.platform, "verify_reply_permission", self.platform.verify_permission)
                          if intent["action"] == "reply" else self.platform.verify_permission)
                allowed = await verify(intent["platform_id"], intent["group_id"])
            except Exception:
                allowed = None
            if allowed is not True:
                outcome = "permission_denied" if allowed is False else "review"
                await run_sync_safely(self.store.complete_attempt, intent_id, intent["attempt"], outcome=outcome,
                                      reason_code="permission_denied" if allowed is False else None)
                return outcome
            guard = await self._acquire_guard(intent_id, intent["attempt"], reply_options=intent.get("reply_options"))
            if guard == "earlier_mute_unresolved":
                await run_sync_safely(self.store.complete_attempt, intent_id, intent["attempt"],
                                      outcome="review", reason_code=guard)
                return "review"
            if guard is None:
                await run_sync_safely(self.store.complete_attempt, intent_id, intent["attempt"], outcome="review")
                return "review"
            retry_allowed = True
            try:
                try:
                    if intent["action"] == "recall":
                        result = await self.platform.recall(intent["platform_id"], intent["group_id"], intent["message_id"])
                    elif intent["action"] == "reply":
                        first_expired = intent["first_reason"] == "quote_expired"
                        passive = intent["attempt"] == 1 or (intent["attempt"] == 2 and not first_expired)
                        options = intent["reply_options"]
                        quoted = options["quote"] and intent["attempt"] < 3
                        # After an expired passive reply, use the last attempt to
                        # drop the reference, or fall back to text if already off.
                        markdown = (intent["attempt"] == 1
                                    or (first_expired and (intent["attempt"] == 2 or options["quote"])))
                        content = intent["template"].format(sender_id=intent["target_sender_id"], rule_id=intent["rule_id"])
                        kwargs = {"mention_id": intent["target_sender_id"]} if markdown and options["mention_sender"] else {}
                        result = await self.platform.send_group_message(intent["platform_id"], intent["group_id"],
                            " ".join(content.splitlines()) if markdown else content,
                            message_id=intent["message_id"] if passive else None,
                            msg_seq=(intent_id % 715827882) * 3 + intent["attempt"] if passive else None,
                            reference_id=intent["message_id"] if quoted else None, markdown=markdown, **kwargs)
                    elif getattr(self.platform, "prepare_mute", None) is not None:
                        target = (intent["platform_id"], intent["group_id"], intent["target_sender_id"])
                        plan = await self.platform.prepare_mute(*target, intent["expires_at"],
                                                               intent["duration_seconds"], retry=attempt > 1)
                        operation = plan.get("operation")
                        result = plan
                        if operation == "update":
                            retry_allowed = False
                            valid = await run_sync_safely(self.store.persist_mute_extension, guard, intent_id,
                                                         intent["attempt"], plan["expires_at"])
                            if valid:
                                result = await self.platform.mute(*target, plan["expires_at"], operation="update")
                            else:
                                result = {"outcome": "review"}
                        elif operation == "add":
                            result = await self.platform.mute(*target, plan["expires_at"])
                    else:
                        result = await self.platform.mute(intent["platform_id"], intent["group_id"],
                                                          intent["target_sender_id"], intent["expires_at"])
                    if not isinstance(result, dict) or result.get("outcome") not in ("succeeded", "failed", "unknown", "blocked", "permission_denied", "review"):
                        result = {"outcome": "unknown"}
                except asyncio.CancelledError:
                    result = {"outcome": "unknown"}
                    raise
                except Exception:
                    result = {"outcome": "unknown"}
            except asyncio.CancelledError:
                await run_sync_safely(guard.close)
                await run_sync_safely(self.store.complete_attempt, intent_id, intent["attempt"], outcome="unknown")
                raise
            else:
                await run_sync_safely(guard.close)
            state = await run_sync_safely(self.store.complete_attempt, intent_id, intent["attempt"],
                                          outcome=result["outcome"], platform_code=result.get("platform_code"),
                                          trace_id=result.get("trace_id"), reason_code=result.get("reason_code"),
                                          retry_allowed=retry_allowed)
            if state == "blocked" and result.get("reason_code") == "protected_target":
                self._notify_rejected(intent)
                await self._send_notice(intent_id)
            if state != "pending_retry":
                return state
            await self.sleep(attempt)
        return "failed"

    async def _send_notice(self, intent_id):
        for index in range(3):
            notice = await run_sync_safely(self.store.begin_notice, intent_id, retry=index > 0)
            if notice is None:
                return
            guard = await self._acquire_guard(intent_id, notice["attempt"], notice=True)
            if guard is None:
                await run_sync_safely(self.store.complete_notice, intent_id, notice["attempt"], outcome="inactive")
                return
            try:
                content = notice["template"].format(sender_id=notice["target_sender_id"], rule_id=notice["rule_id"], action=notice["action"])
                if notice["send_method"] == "markdown":
                    content = " ".join(content.splitlines())
                result = await self.platform.send_group_message(notice["platform_id"], notice["group_id"], content,
                    reference_id=notice["message_id"] if notice["sent_quoted"] else None,
                    message_id=notice["message_id"] if notice["sent_passive"] else None,
                    msg_seq=(intent_id % 715827882) * 3 + notice["attempt"] if notice["sent_passive"] else None,
                    markdown=notice["send_method"] == "markdown")
                if not isinstance(result, dict) or result.get("outcome") not in ("succeeded", "failed", "unknown"):
                    result = {"outcome": "unknown"}
            except asyncio.CancelledError:
                await run_sync_safely(guard.close)
                await run_sync_safely(self.store.complete_notice, intent_id, notice["attempt"], outcome="unknown")
                raise
            except Exception:
                result = {"outcome": "unknown"}
            await run_sync_safely(guard.close)
            state = await run_sync_safely(self.store.complete_notice, intent_id, notice["attempt"],
                outcome=result["outcome"], platform_code=result.get("platform_code"), trace_id=result.get("trace_id"),
                reason_code=result.get("reason_code"))
            if state != "pending_retry":
                return
            await self.sleep(1)
