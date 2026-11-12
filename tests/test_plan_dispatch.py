import asyncio
import unittest
from types import SimpleNamespace

from sentinel.dry_run_host import DryRunHost
from sentinel.dry_run_queue import DryRunQueue


class PlanDispatchTests(unittest.IsolatedAsyncioTestCase):
    def test_aggregate_states(self):
        cases = ((["succeeded", "succeeded", "succeeded"], "succeeded"),
                 (["succeeded", "failed", "succeeded"], "partial_failed"),
                 (["failed", "failed"], "failed"),
                 (["blocked", "permission_denied"], "blocked"),
                 (["succeeded", "unknown"], "unknown"),
                 (["succeeded", "pending_confirm"], "pending_confirm"),
                 (["pending_auto", "pending_confirm"], "pending_confirm"),
                 (["failed", "pending_confirm"], "pending_confirm"),
                 (["unknown", "pending_confirm"], "pending_confirm"))
        for states, expected in cases:
            with self.subTest(states=states):
                self.assertEqual(DryRunQueue._execution_summary([{"status": state} for state in states]), expected)

    async def test_automatic_actions_are_ordered_and_confirmation_waits_for_them(self):
        calls = []
        pending = [1, 3]
        first_started, release_first = asyncio.Event(), asyncio.Event()

        class Store:
            def plan_for_intent(self, intent_id):
                return "plan"

            def pending_plan_auto(self, plan_id):
                return list(pending)

        class Executor:
            store = Store()

            async def execute(self, intent_id, *, mode):
                calls.append((intent_id, mode))
                if mode == "auto":
                    pending.remove(intent_id)
                if intent_id == 1:
                    first_started.set()
                    await release_first.wait()
                    return "blocked"
                return "succeeded"

        host = DryRunHost(context=None, config={}, rules=None, queue=SimpleNamespace(), lock_path="unused")
        host.mute_executor = Executor()
        task = await host._schedule_plan(1)
        await first_started.wait()
        confirm = asyncio.create_task(host.confirm_mute(2))
        await asyncio.sleep(0)
        self.assertEqual(calls, [(1, "auto")])
        release_first.set()
        await task
        self.assertEqual(await confirm, "succeeded")
        self.assertEqual(calls, [(1, "auto"), (3, "auto"), (2, "confirm")])


if __name__ == "__main__":
    unittest.main()
