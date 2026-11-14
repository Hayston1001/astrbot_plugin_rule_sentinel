"""Injectable mute execution coordinator."""

import asyncio

from .async_io import run_sync_safely


class MuteExecutor:
    def __init__(self, store, platform, *, sleep=asyncio.sleep):
        self.store, self.platform, self.sleep = store, platform, sleep

    async def _acquire_guard(self, intent_id, attempt, *, notice=False):
        acquire = self.store.acquire_notice_guard if notice else self.store.acquire_send_guard
        task = asyncio.create_task(asyncio.to_thread(acquire, intent_id, attempt))
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
                if guard is not None and guard != "recall_expired":
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
            guard = await self._acquire_guard(intent_id, intent["attempt"])
            if guard == "recall_expired":
                await run_sync_safely(self.store.complete_attempt, intent_id, intent["attempt"], outcome="blocked", reason_code="recall_expired")
                return "blocked"
            if guard is None:
                await run_sync_safely(self.store.complete_attempt, intent_id, intent["attempt"], outcome="review")
                return "review"
            try:
                try:
                    if intent["action"] == "recall":
                        result = await self.platform.recall(intent["platform_id"], intent["group_id"], intent["message_id"])
                    elif intent["action"] == "reply":
                        first_expired = intent["first_reason"] == "quote_expired"
                        passive = intent["attempt"] == 1 or (intent["attempt"] == 2 and not first_expired)
                        quoted = intent["attempt"] < 3
                        markdown = intent["attempt"] != 2 or first_expired
                        content = intent["template"].format(sender_id=intent["target_sender_id"], rule_id=intent["rule_id"])
                        result = await self.platform.send_group_message(intent["platform_id"], intent["group_id"],
                            " ".join(content.splitlines()) if markdown else content,
                            message_id=intent["message_id"] if passive else None,
                            msg_seq=(intent_id % 715827882) * 3 + intent["attempt"] if passive else None,
                            reference_id=intent["message_id"] if quoted else None, markdown=markdown)
                    else:
                        result = await self.platform.mute(intent["platform_id"], intent["group_id"],
                                                          intent["target_sender_id"], intent["expires_at"])
                    if not isinstance(result, dict) or result.get("outcome") not in ("succeeded", "failed", "unknown", "blocked", "permission_denied"):
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
                                          trace_id=result.get("trace_id"), reason_code=result.get("reason_code"))
            if state == "blocked" and result.get("reason_code") == "protected_target":
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
