"""Tests for routing OpenClaw background results through AstrBot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot.api.message_components import At, Plain
from astrbot.api.platform import MessageType

from core.runner import _try_relay_result_via_astrbot, background_run


class _Queue:
    def __init__(self) -> None:
        self.items = []

    def put_nowait(self, item) -> None:
        self.items.append(item)


class _Platform:
    def __init__(self) -> None:
        self.message = None

    def create_event(self, message):
        self.message = message
        return SimpleNamespace(is_wake=False, is_at_or_wake_command=False)


class _Context:
    def __init__(self, platform: _Platform, queue: _Queue) -> None:
        self.platform = platform
        self.queue = queue

    def get_platform_inst(self, platform_id: str):
        return self.platform if platform_id == "test-platform" else None

    def get_event_queue(self) -> _Queue:
        return self.queue


class _Event:
    def __init__(self) -> None:
        self.message_obj = SimpleNamespace(raw_message={"source": "original"})
        self.direct_messages = []

    def get_platform_id(self) -> str:
        return "test-platform"

    def get_session_id(self) -> str:
        return "group-42"

    def get_sender_id(self) -> str:
        return "user-7"

    def get_sender_name(self) -> str:
        return "Test User"

    def get_self_id(self) -> str:
        return "bot-9"

    def get_message_type(self) -> MessageType:
        return MessageType.GROUP_MESSAGE

    def get_group_id(self) -> str:
        return "group-42"

    async def send(self, message) -> None:
        self.direct_messages.append(message)


class _TaskLog:
    def __init__(self) -> None:
        self.rows = []

    def insert(self, info: dict) -> None:
        self.rows.append(info.copy())

    def update(self, info: dict) -> None:
        self.rows.append(info.copy())


def test_background_result_is_requeued_for_astrbot_relay() -> None:
    """The relay path creates a wake event in the original user session."""
    platform = _Platform()
    queue = _Queue()
    context = _Context(platform, queue)

    sent = asyncio.run(
        _try_relay_result_via_astrbot(
            _Event(),
            "Please relay this result.",
            task_id="bg-123",
            context=context,
            kind="done",
        )
    )

    assert sent is True
    assert len(queue.items) == 1
    assert queue.items[0].is_wake is True
    assert queue.items[0].is_at_or_wake_command is True
    assert platform.message.type is MessageType.GROUP_MESSAGE
    assert platform.message.session_id == "group-42"
    assert platform.message.sender.user_id == "user-7"
    assert platform.message.group_id == "group-42"
    assert platform.message.message_id == "openclaw-bg-123"
    assert isinstance(platform.message.message[0], At)
    assert platform.message.message[0].qq == "bot-9"
    assert isinstance(platform.message.message[1], Plain)
    assert platform.message.message[1].text == "Please relay this result."


def test_background_run_uses_relay_instead_of_direct_send() -> None:
    """Relay mode queues the result and does not send it from the old event."""
    platform = _Platform()
    queue = _Queue()
    context = _Context(platform, queue)
    event = _Event()
    task_log = _TaskLog()

    async def call_openclaw(**_kwargs) -> str:
        return "OpenClaw completed the requested work."

    asyncio.run(
        background_run(
            task="Do the requested work.",
            session_key="session-key",
            user_id="user-7",
            project="research",
            task_id="bg-456",
            event=event,
            call_openclaw=call_openclaw,
            task_log=task_log,
            bg_tasks={},
            task_handles={},
            context=context,
            relay_via_astrbot=True,
        )
    )

    assert event.direct_messages == []
    assert task_log.rows[-1]["status"] == "done"
    assert len(queue.items) == 1
    assert "不要重复执行任务" in platform.message.message_str
    assert "OpenClaw completed the requested work." in platform.message.message_str
