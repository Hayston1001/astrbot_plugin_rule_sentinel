"""Bounded per-group audit subscriptions owned by one event loop."""

import asyncio
import json

from .dry_run_queue import SYSTEM_PLATFORM


class EventHub:
    def __init__(self, loop, limit=4, capacity=100):
        self.loop, self.limit, self.capacity = loop, limit, capacity
        self.subscribers = set()
        self.closed = False

    def subscribe(self, platform_id, group_id):
        if self.closed or len(self.subscribers) >= self.limit:
            raise RuntimeError("subscription limit reached")
        subscription = Subscription(self, None if platform_id is None and group_id is None else (platform_id, group_id))
        self.subscribers.add(subscription)
        return subscription

    def publish(self, platform_id, group_id, message_id, event_id):
        try:
            self.loop.call_soon_threadsafe(self._deliver, (platform_id, group_id), message_id, event_id)
        except RuntimeError:
            pass

    def _deliver(self, group, message_id, event_id):
        for sub in tuple(self.subscribers):
            if (sub.group is None and group[0] == SYSTEM_PLATFORM) or (sub.group is not None and sub.group != group) or sub.overflow:
                continue
            try:
                sub.queue.put_nowait((group, message_id, event_id))
            except asyncio.QueueFull:
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.overflow = True
                sub.queue.put_nowait("resync")

    def close(self):
        self.closed = True
        for sub in tuple(self.subscribers):
            sub.close()


class Subscription:
    def __init__(self, hub, group):
        self.hub, self.group = hub, group
        self.queue = asyncio.Queue(maxsize=hub.capacity)
        self.overflow = False
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.hub.subscribers.discard(self)
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)

    async def stream(self, queue):
        try:
            yield "event: ping\ndata: {}\n\n"
            loop = asyncio.get_running_loop()
            next_ping = loop.time() + 15
            while not self.closed:
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=max(0, next_ping - loop.time()))
                except TimeoutError:
                    next_ping = loop.time() + 15
                    yield "event: ping\ndata: {}\n\n"
                    continue
                if item is None:
                    break
                if item == "resync":
                    self.overflow = False
                    yield "event: resync\ndata: {}\n\n"
                    continue
                group, message_id, event_id = item
                row = await asyncio.to_thread(queue.message_at_event, *group, message_id, event_id)
                if row is not None:
                    yield "data: " + json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n\n"
        finally:
            self.close()
