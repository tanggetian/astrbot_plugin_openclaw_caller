"""session_key 生成、system_prompt 渲染、项目名归一化、/oc prompt 解析。

全部是纯函数——不依赖任何模块级可变状态（系统提示词模板、session 计数器等
作为参数传入）。
"""
from __future__ import annotations

import hashlib
import re


def render_openclaw_system_prompt(
    template: str, project: str, user_id: str, session_key: str
) -> str:
    """渲染 system message 模板。

    支持 ``{project}`` / ``{{project}}`` 两种占位符。
    """
    text = template or ""
    replacements = {
        "project": project or "general",
        "user_id": user_id or "",
        "session_key": session_key or "",
    }
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value).replace("{" + key + "}", value)
    return text.strip()


def system_prompt_session_suffix(system_prompt_template: str) -> str:
    """根据 system_prompt 模板内容计算 session_key 后缀。

    模板变更 → 自动切换 session_key 后缀 → OpenClaw 端开新 session，避免老 session 残留。
    """
    template = (system_prompt_template or "").strip()
    if not template:
        return ""
    digest = hashlib.sha1(template.encode("utf-8")).hexdigest()[:8]
    return f"-sp{digest}"


def event_platform_key(event) -> str:
    """从 AstrMessageEvent 提取稳定的平台标识（多 bot 隔离用）。

    优先 ``get_platform_id``，回退 ``get_platform_name``，都拿不到时返回 "unknown"。
    """
    platform = "unknown"
    if event is not None:
        for method in ("get_platform_id", "get_platform_name"):
            try:
                value = getattr(event, method)()
                if value:
                    platform = str(value)
                    break
            except Exception:
                pass
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", platform).strip("-").lower() or "unknown"


def extract_send_target(event) -> dict:
    """从 event 抓出 (platform_name, session_id) — 后台任务完成时推送用。

    为什么需要这个：
    - event.send() 在 AstrBot 里绑的是**原消息**的生命周期，event finalize 之后再 send
      可能静默丢消息（无异常），用户就收不到结果
    - 平台适配器（context.get_platform().send_message()）不绑 event 生命周期，是
      延迟推送的标准姿势

    Returns:
        ``{"platform_name": str, "session_id": str}``；任一拿不到时返回 ``{}``。
        调用方看到空 dict 时应**跳过**平台 fallback（不报错，避免无意义的日志噪声）。
    """
    if event is None:
        return {}
    platform_name = ""
    for method in ("get_platform_id", "get_platform_name"):
        try:
            value = getattr(event, method, lambda: "")()
            if value:
                platform_name = str(value)
                break
        except Exception:
            pass
    session_id = ""
    try:
        value = getattr(event, "get_session_id", lambda: "")()
        if value:
            session_id = str(value)
    except Exception:
        pass
    if not platform_name or not session_id:
        return {}
    return {
        "platform_name": re.sub(r"[^a-zA-Z0-9_-]+", "-", platform_name).strip("-").lower(),
        "session_id": session_id,
    }


def make_session_key(
    platform: str,
    sender_id: str,
    mode: str,
    sp_suffix: str,
    session_counters: dict[str, int],
) -> str:
    """构造 session_key。

    形如 ``<platform>-<sender_id>-<mode><sp_suffix>``；如果该 base 已被 reset 过
    （counter>0），则后缀 ``-rN``。
    """
    base = f"{platform}-{sender_id}-{mode}{sp_suffix}"
    counter = session_counters.get(base, 0)
    if counter > 0:
        return f"{base}-r{counter}"
    return base


def make_next_session_key(
    platform: str,
    sender_id: str,
    mode: str,
    sp_suffix: str,
    session_counters: dict[str, int],
) -> str:
    """生成下一个 session_key（/oc reset 用），同时更新计数器。"""
    base = f"{platform}-{sender_id}-{mode}{sp_suffix}"
    counter = session_counters.get(base, 0) + 1
    session_counters[base] = counter
    return f"{base}-r{counter}"


def normalize_project(project: str) -> str:
    """归一化项目名（**完全动态**——不限制清单）。

    规则：
    - 空 → fallback "general"
    - 合法格式（a-z 开头 + a-z0-9-_+ 1-31 字符）→ 接受
    - 其它格式 → fallback "general"

    **LLM 自主传任何字符串都行**——任何项目名都形成独立 session。
    """
    p = (project or "").strip().lower()
    if not p:
        return "general"
    if re.match(r"^[a-z][a-z0-9_\-+]{0,30}$", p):
        return p
    return "general"


def parse_oc_prompt(prompt: str) -> tuple[str, str]:
    """解析 /oc [project] <任务> 格式（**完全动态**）。

    Returns:
        (实际任务文本, 项目名)
    """
    stripped = prompt.strip()
    if not stripped:
        return "", "general"
    first_word = stripped.split(maxsplit=1)[0].lower()
    if first_word and re.match(r"^[a-z][a-z0-9_\-+]{0,30}$", first_word):
        real_prompt = stripped.split(maxsplit=1)[1] if " " in stripped else ""
        return real_prompt, first_word
    return prompt, "general"
