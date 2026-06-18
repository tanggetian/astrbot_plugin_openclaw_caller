"""白名单访问控制。

- whitelist_enabled=False → 全部放行（返回 True）
- whitelist_enabled=True + sender in allowed_user_ids → 放行（返回 True）
- whitelist_enabled=True + sender not in allowed_user_ids → 拦截（返回 False）
  - block_when_disabled=True → 静默（不提示）
  - block_when_disabled=False → 提示『不在白名单』

**首次使用需在 WebUI 把允许的用户 ID 填到 allowed_user_ids 列表**。
返回 True 表示放行，False 表示被拦截（调用方应 return）。
"""
from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain


async def check_allowed(
    whitelist_enabled: bool,
    allowed_user_ids: set[str],
    block_when_disabled: bool,
    event: AstrMessageEvent,
) -> bool:
    if not whitelist_enabled:
        return True
    sender_id = ""
    try:
        sender_id = str(event.get_sender_id() or "")
    except Exception:
        sender_id = ""
    allowed = allowed_user_ids or set()
    if sender_id in {str(x) for x in allowed}:
        return True
    if not block_when_disabled:
        try:
            await event.send(MessageChain([Plain("该功能不在您的白名单中")]))
        except Exception:
            pass
    return False
