"""In-process event bus. Services publish messages; WebSocket clients and services subscribe.

Messages are plain JSON-ready dicts with a `type` key, e.g.
`{"type": "toast", "data": {"text": "Paired", "level": "info"}}`. See docs/protocol.md.

Each subscriber gets its own bounded queue. A subscriber that falls too far behind is closed,
not blocked on: the UI reconnects and gets a fresh snapshot, which is simpler and safer than
letting one stuck client hold up the remote.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

Message = dict[str, Any]

_CLOSED: Message = {"type": "__closed__"}


class Subscription:
    def __init__(self, bus: "EventBus", maxsize: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Message] = asyncio.Queue(maxsize + 1)  # +1 for _CLOSED
        self._maxsize = maxsize
        self.closed = False
        self.overflowed = False

    def _offer(self, message: Message) -> None:
        if self.closed:
            return
        if self._queue.qsize() >= self._maxsize:
            log.warning("subscriber fell %d messages behind, dropping it", self._maxsize)
            self.overflowed = True
            self._close()
            return
        self._queue.put_nowait(message)

    def _close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._bus._subscribers.discard(self)
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(_CLOSED)

    def close(self) -> None:
        self._close()

    async def get(self) -> Message | None:
        """Next message, or None once the subscription is closed."""
        message = await self._queue.get()
        return None if message is _CLOSED else message

    def __aiter__(self) -> AsyncIterator[Message]:
        return self

    async def __anext__(self) -> Message:
        message = await self.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    def subscribe(self, maxsize: int = 256) -> Subscription:
        sub = Subscription(self, maxsize)
        self._subscribers.add(sub)
        return sub

    def publish(self, type: str, data: Any = None, **extra: Any) -> None:
        message: Message = {"type": type, "data": data, **extra}
        for sub in list(self._subscribers):
            sub._offer(message)

    def toast(self, text: str, level: str = "info") -> None:
        self.publish("toast", {"text": text, "level": level})

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
