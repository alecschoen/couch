import asyncio

from couchd.bus import EventBus


async def test_subscriber_gets_messages_in_order():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish("key", {"key": "up"})
    bus.publish("key", {"key": "down"})
    assert await sub.get() == {"type": "key", "data": {"key": "up"}}
    assert await sub.get() == {"type": "key", "data": {"key": "down"}}


async def test_every_subscriber_gets_every_message():
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.toast("hello")
    expected = {"type": "toast", "data": {"text": "hello", "level": "info"}}
    assert await a.get() == expected
    assert await b.get() == expected


async def test_extra_fields_are_merged_into_the_message():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish("state", {"audio": {}}, full=False)
    assert await sub.get() == {"type": "state", "data": {"audio": {}}, "full": False}


async def test_publish_without_subscribers_is_fine():
    EventBus().publish("toast", {"text": "nobody home"})


async def test_closed_subscription_stops_receiving():
    bus = EventBus()
    with bus.subscribe() as sub:
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0
    bus.publish("key", {"key": "up"})
    assert await sub.get() is None


async def test_close_wakes_a_waiting_reader():
    bus = EventBus()
    sub = bus.subscribe()
    reader = asyncio.create_task(sub.get())
    await asyncio.sleep(0)
    sub.close()
    assert await asyncio.wait_for(reader, 1) is None


async def test_async_iteration_ends_on_close():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish("key", {"key": "ok"})
    sub.close()
    assert [m async for m in sub] == []  # pending messages are dropped on close


async def test_slow_subscriber_is_dropped_not_blocking():
    bus = EventBus()
    slow = bus.subscribe(maxsize=3)
    fast = bus.subscribe(maxsize=100)
    for i in range(5):
        bus.publish("key", {"key": str(i)})
    assert slow.overflowed
    assert slow.closed
    assert await slow.get() is None
    assert bus.subscriber_count == 1
    assert [(await fast.get())["data"]["key"] for _ in range(5)] == ["0", "1", "2", "3", "4"]
