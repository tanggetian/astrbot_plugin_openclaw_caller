"""LiteEvent mock——当 AstrBot 框架没有给 Tool 注入真 AstrMessageEvent 时 fallback 用。

背景：
- delegate_to_openclaw 正常应拿到 AstrBot 注入的真 AstrMessageEvent；
- 若框架或调用路径没有提供真 event，则 fallback 到 LiteEvent 供 session_key 解析 / 元信息查询使用。
- 随后 _background_run 会识别 LiteEvent，并把任务标为 no_recipient 而非假装已推送。

关键约束：
- send / send_typing / stop_typing 必须是 async 函数（await 不报错）
- get_sender_id 必须返回**真** sender_id——白名单校验靠它
- get_extra / set_extra 用 dict 内部存——后续扩展调用不爆 AttributeError
  （**用 staticmethod 包装**——否则 ev.set_extra("k", "v") 会 TypeError）
- 标记 _is_lite=True，上游代码据此识别这是 mock 而非真 event
"""
from __future__ import annotations


def make_lite_event(sender_id: str) -> object:
    """构造一个"方法齐全"的 LiteEvent mock。"""
    extras: dict = {}

    async def _noop_async(*_a, **_kw):
        return None

    def _get_sender_id():
        return sender_id

    def _get_session_id():
        return sender_id

    def _get_message_id():
        return ""

    def _get_message_str():
        return ""

    def _get_message_outline():
        return ""

    def _get_platform_id():
        return "lite"

    def _get_self_id():
        return "lite"

    def _get_platform_name():
        return "lite"

    def _is_at_or_wake_command(*_a, **_kw):
        return False

    def _get_extra(key, default=None):
        return extras.get(key, default)

    def _set_extra(key, val):
        extras[key] = val

    cls = type("LiteEvent", (), {
        "send": _noop_async,
        "send_typing": _noop_async,
        "stop_typing": _noop_async,
        "get_sender_id": staticmethod(_get_sender_id),
        "get_session_id": staticmethod(_get_session_id),
        "get_message_id": staticmethod(_get_message_id),
        "get_message_str": staticmethod(_get_message_str),
        "get_message_outline": staticmethod(_get_message_outline),
        "get_platform_id": staticmethod(_get_platform_id),
        "get_self_id": staticmethod(_get_self_id),
        "get_platform_name": staticmethod(_get_platform_name),
        "is_at_or_wake_command": staticmethod(_is_at_or_wake_command),
        "get_extra": staticmethod(_get_extra),
        "set_extra": staticmethod(_set_extra),
        "_is_lite": True,
    })
    return cls()
