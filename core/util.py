"""常量、异常、错误脱敏、布尔转换等纯函数工具。"""
from __future__ import annotations

import hashlib

PLUGIN_NAME = "astrbot_plugin_openclaw_caller"


class OpenClawError(RuntimeError):
    """OpenClaw 调用相关错误基类。"""


def sanitize_error(e: Exception) -> str:
    """对用户/LLM/WebUI 隐藏异常细节——避免泄露 OpenClaw URL/Token/内部路径。

    完整 str(e) + traceback 走 logger.error(..., exc_info=True) 由管理员从日志看。
    用户/工具调用方只拿到「异常类型 + 提示看日志」。
    """
    return f"{type(e).__name__}（详情见 AstrBot 日志）"


def to_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if value is None:
        return default
    return bool(value)


def digest(value: str, length: int = 8) -> str:
    """把 sender_id / session_key 等敏感字段哈希成可关联但不可还原的短标签。

    用途：日志里要把同一用户/同一 session 的多条记录串起来看，但**不暴露**原值。
    例：``digest("user-12345") == "a1b2c3d4"``——两个 log 用同一 digest 即表示同一用户。
    """
    if not value:
        return ""
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:length]


def new_request_id() -> str:
    """生成 8 字符的请求 ID——给一次 OpenClaw 调用做关联 key。

    在 client.py 起头生成；start / end / error 三条日志都带它，管理员
    ``grep "request_id=abc12345"`` 即可拉出一次调用的完整生命周期。
    """
    import uuid
    return uuid.uuid4().hex[:8]
