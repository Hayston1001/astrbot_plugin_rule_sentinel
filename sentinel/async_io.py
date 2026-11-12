"""Helpers for waiting for owned work during cancellation."""

import asyncio


async def run_sync_safely(function, /, *args, **kwargs):
    """Do not abandon a thread that may still be writing persistent state."""
    return await run_awaitable_safely(asyncio.to_thread(function, *args, **kwargs))


async def run_awaitable_safely(awaitable):
    """Finish an owned operation before propagating repeated cancellation."""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A second cancellation may arrive during shutdown. Continue waiting
        # until the thread completes before allowing lock ownership to end.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise
