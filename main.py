"""
astrbot_plugin_openclaw_caller

让 AstrBot 主 LLM通过 Function Calling 自主调度 OpenClaw Gateway。
- 主人不用打 /oc
- AstrBot 主 LLM 判断长任务时自动外包给 OpenClaw
- 流式返回，体验好
- 会话隔离，主人独占
"""
import asyncio
import json
import re
import sqlite3
import uuid
import time
import hashlib
from pathlib import Path
import aiohttp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from quart import jsonify  # Plugin Page API 用
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# === 插件元信息（模块级常量） ===
PLUGIN_NAME = "astrbot_plugin_openclaw_caller"

# === 配置（无 hardcode）===
# 所有配置从 self.config 读，_conf_schema.json 定义 default
# 用户必须在 AstrBot WebUI 里填 openclaw_url 和 openclaw_token


def _make_lite_event(sender_id: str) -> object:
    """构造一个"方法齐全"的 LiteEvent mock

    背景：delegate_to_openclaw 正常应拿到 AstrBot 注入的真 AstrMessageEvent；
    若框架或调用路径没有提供真 event，则 fallback 到 LiteEvent 供 session_key 解析 / 元信息查询使用。
    随后 _background_run_module 会识别 LiteEvent，并把任务标为 no_recipient 而非假装已推送。

    关键约束：
    - send / send_typing / stop_typing 必须是 async 函数（await 不报错）
    - get_sender_id 必须返回**真** sender_id——白名单校验靠它
    - get_extra / set_extra 用 dict 内部存——后续扩展调用不爆 AttributeError
    - 标记 _is_lite=True，上游代码据此识别这是 mock 而非真 event
    """
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
        "get_extra": _get_extra,
        "set_extra": _set_extra,
        "_is_lite": True,
    })
    return cls()


def _sanitize_error(e: Exception) -> str:
    """对用户/LLM/WebUI 隐藏异常细节——避免泄露 OpenClaw URL/Token/内部路径

    完整 str(e) + traceback 走 logger.error(..., exc_info=True) 由管理员从日志看。
    用户/工具调用方只拿到「异常类型 + 提示看日志」。
    """
    return f"{type(e).__name__}（详情见 AstrBot 日志）"


class OpenClawError(RuntimeError):
    pass


def _to_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if value is None:
        return default
    return bool(value)


def _render_openclaw_system_prompt(template: str, project: str, user_id: str, session_key: str) -> str:
    text = template or ""
    replacements = {
        "project": project or "general",
        "user_id": user_id or "",
        "session_key": session_key or "",
    }
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value).replace("{" + key + "}", value)
    return text.strip()


def _system_prompt_session_suffix() -> str:
    template = (_module_cfg.get("openclaw_system_prompt", "") or "").strip()
    if not template:
        return ""
    digest = hashlib.sha1(template.encode("utf-8")).hexdigest()[:8]
    return f"-sp{digest}"


# 模块级配置缓存（__init__ 时同步）
# 原因：@filter.llm_tool 调用的 Tool 函数 self 不可用，需走模块级状态
_module_cfg: dict = {
    "whitelist_enabled": True,
    "allowed_user_ids": set(),
    "block_when_disabled": False,
    "openclaw_url": "",
    "openclaw_token": "",
    "openclaw_agent_id": "main",
    "openclaw_timeout": 300,
    "openclaw_verify_ssl": True,
    "task_handles": {},
    "initialized_sessions": set(),
}


async def _check_allowed(plugin, event: AstrMessageEvent) -> bool:
    """检查发送者是否在白名单里（access_control 配置项，**默认白名单开启**）。

    - whitelist_enabled=False → 全部放行（返回 True）
    - whitelist_enabled=True + sender in allowed_user_ids → 放行（返回 True）
    - whitelist_enabled=True + sender not in allowed_user_ids → 拦截（返回 False）
      - block_when_disabled=True → 静默（不提示）
      - block_when_disabled=False → 提示『不在白名单』

    **首次使用需在 WebUI 把允许的用户 ID 填到 allowed_user_ids 列表**。

    返回 True 表示放行，False 表示被拦截（调用方应 return）。
    """
    if not plugin.whitelist_enabled:  # 白名单关闭
        return True
    sender_id = ""
    try:
        sender_id = str(event.get_sender_id() or "")
    except Exception:
        sender_id = ""
    allowed = plugin.allowed_user_ids or set()
    if sender_id in {str(x) for x in allowed}:
        return True
    # 不在白名单
    if not plugin.block_when_disabled:
        try:
            await event.send(MessageChain([Plain("该功能不在您的白名单中")]))
        except Exception:
            pass
    return False


def _get_db_path() -> Path:
    """获取插件独立 SQLite 数据库路径。"""
    data_dir = Path(get_astrbot_data_path()) / "plugins" / PLUGIN_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "openclaw_caller.db"


def _init_db() -> None:
    """确保 openclaw_sessions 表存在"""
    db = _get_db_path()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS openclaw_sessions (
                user_id    TEXT NOT NULL,
                session_key TEXT NOT NULL,
                messages   TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, session_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS openclaw_tasks (
                task_id      TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                project      TEXT NOT NULL,
                task_text    TEXT NOT NULL,
                status       TEXT NOT NULL,
                mode         TEXT NOT NULL,
                created_at   REAL NOT NULL,
                finished_at  REAL,
                result_text  TEXT,
                error_text   TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON openclaw_tasks(created_at DESC)")
        conn.commit()
    finally:
        conn.close()


def _load_session(user_id: str, session_key: str) -> list[dict]:
    """加载主人在这个 session_key 的历史 messages"""
    _init_db()
    db = _get_db_path()
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT messages FROM openclaw_sessions WHERE user_id=? AND session_key=?",
            (user_id, session_key)
        ).fetchone()
    finally:
        conn.close()
    if row:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _save_session(user_id: str, session_key: str, messages: list[dict]) -> None:
    """保存历史到 SQLite"""
    _init_db()
    db = _get_db_path()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO openclaw_sessions (user_id, session_key, messages, updated_at) VALUES (?,?,?,?)",
            (user_id, session_key, json.dumps(messages, ensure_ascii=False), int(time.time()))
        )
        conn.commit()
    finally:
        conn.close()


def _clear_session(user_id: str, session_key: str) -> None:
    """清空会话历史（/oc reset 用）"""
    _module_cfg.get("initialized_sessions", set()).discard(session_key)
    _init_db()
    db = _get_db_path()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "DELETE FROM openclaw_sessions WHERE user_id=? AND session_key=?",
            (user_id, session_key)
        )
        conn.commit()
    finally:
        conn.close()


async def call_openclaw(
    message: str,
    session_key: str,
    user_id: str = "default",
    project: str = "general",
    openclaw_url: str = "",
    openclaw_token: str = "",
    openclaw_agent_id: str = "main",
    openclaw_timeout: int = 300,
    openclaw_verify_ssl: bool = True,
) -> str:
    """调 OpenClaw Gateway /v1/chat/completions（OpenAI 协议，多轮对话）

    主人这台 OpenClaw 走的是标准 OpenAI Chat Completions 协议。
    多轮对话：每次从 SQLite 读历史 messages，调完把 assistant 回复 push 进去再存。
    """
    # 用传入参数或 _module_cfg 默认值（__init__ 同步写入）
    url = openclaw_url or _module_cfg.get("openclaw_url", "")
    token = openclaw_token or _module_cfg.get("openclaw_token", "")
    agent_id = openclaw_agent_id or _module_cfg.get("openclaw_agent_id", "main")
    timeout_seconds = openclaw_timeout or _module_cfg.get("openclaw_timeout", 300)
    # SSL 验证默认开——OpenClaw 部署在公网时保护 Bearer Token
    # 仅当用户**显式**在配置里关闭（_conf_schema.json.openclaw_verify_ssl=false）才不走证书校验
    verify_ssl = openclaw_verify_ssl if isinstance(openclaw_verify_ssl, bool) \
        else _module_cfg.get("openclaw_verify_ssl", True)

    # 兜底：如果 base URL 没路径，自动追加 /v1/chat/completions
    if url and not url.rstrip("/").endswith("/chat/completions"):
        url = url.rstrip("/") + "/v1/chat/completions"

    # 构造 messages：只发当前 user message；system prompt 仅在 session_key 首次开始时发送一次。
    # 首轮采用 system role + user 首部内嵌双保险：有些 OpenAI 兼容 Gateway 不会把 system role 写入长期 session。
    # OpenClaw 自己按 user 字段维护多轮 session，桥接层不重发历史，避免上下文重复注入 + 隐私泄漏。
    messages: list[dict] = []
    audit_messages = _load_session(user_id, session_key)
    initialized_sessions = _module_cfg.setdefault("initialized_sessions", set())
    is_session_started = bool(audit_messages) or session_key in initialized_sessions
    _sp = _render_openclaw_system_prompt(
        _module_cfg.get("openclaw_system_prompt", ""),
        project,
        user_id,
        session_key,
    )
    just_initialized_session = False
    if _sp and not is_session_started:
        messages.append({"role": "system", "content": _sp})
        initialized_sessions.add(session_key)
        just_initialized_session = True
    current_message = message
    if just_initialized_session:
        current_message = (
            "【OpenClaw 项目初始化指令】\n"
            f"{_sp}\n"
            "【初始化指令结束】\n\n"
            "下面是本项目的第一条实际任务：\n"
            f"{message}"
        )
    messages.append({"role": "user", "content": current_message})
    session_digest = hashlib.sha1(session_key.encode("utf-8")).hexdigest()[:8]
    logger.info(
        f"[OpenClaw] payload prepared: project={project}, "
        f"session_digest={session_digest}, system_prompt_configured={bool(_sp)}, "
        f"system_prompt_injected={just_initialized_session}"
    )

    payload = {
        "model": f"openclaw:{agent_id}",
        "messages": messages,  # system（可选）+ user 当前
        "user": session_key,  # OpenClaw 按这个字段 session 隔离
        "stream": True,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    chunks = []
    first_chunk_time = None
    import time as _time
    t0 = _time.time()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        # SSL 验证：默认开（保护 Bearer Token），仅当 openclaw_verify_ssl=false 才关
        if verify_ssl:
            connector = aiohttp.TCPConnector()
        else:
            logger.warning(
                "[OpenClaw] SSL 证书校验已关闭（_conf_schema.json.openclaw_verify_ssl=false）"
                "——Bearer Token 将以明文在网络中传输，仅限本地/自签名证书场景"
            )
            connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"[OpenClaw HTTP {resp.status}] {body[:1000]}")
                    raise OpenClawError(f"OpenClaw HTTP {resp.status}")
                content_type = resp.headers.get("Content-Type", "")
                if content_type.startswith("text/event-stream"):
                    # SSE 流式解析（OpenAI Chat Completions 协议）
                    buf = b""
                    SSE_BUF_MAX = 1_048_576  # 1 MB——防 Gateway bug（一直不换行）把内存撑爆
                    done = False
                    async for raw_line in resp.content:
                        buf += raw_line
                        # 兜底：单条消息超 1MB 还没遇到换行，截断丢弃
                        if len(buf) > SSE_BUF_MAX:
                            logger.warning(
                                f"[OpenClaw] SSE buffer 超过 {SSE_BUF_MAX} 字节未换行，截断"
                            )
                            buf = b""
                            continue
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.decode("utf-8", errors="ignore").strip()
                            if not line or line.startswith(":"):
                                continue
                            if line.startswith("data:"):
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    done = True
                                    break
                                try:
                                    evt = json.loads(data)
                                    if isinstance(evt, dict) and "choices" in evt:
                                        delta = evt["choices"][0].get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            if first_chunk_time is None:
                                                first_chunk_time = _time.time() - t0
                                                logger.info(f"[OpenClaw] 首字延迟 {first_chunk_time:.2f}s")
                                            chunks.append(content)
                                except json.JSONDecodeError:
                                    pass
                        if done:
                            break
                else:
                    # 普通 JSON（OpenAI 格式）
                    body = await resp.text()
                    try:
                        obj = json.loads(body)
                        if isinstance(obj, dict) and "choices" in obj:
                            chunks.append(obj["choices"][0]["message"]["content"])
                    except Exception:
                        chunks.append(body[:1000])
    except asyncio.TimeoutError:
        if just_initialized_session:
            initialized_sessions.discard(session_key)
        raise OpenClawError(f"OpenClaw 请求超时（{timeout_seconds}s）")
    except aiohttp.ClientError as e:
        if just_initialized_session:
            initialized_sessions.discard(session_key)
        logger.error(f"[OpenClaw] 连接错误: {e}", exc_info=True)
        raise OpenClawError("OpenClaw 连接错误") from e
    except Exception as e:
        if just_initialized_session:
            initialized_sessions.discard(session_key)
        if isinstance(e, OpenClawError):
            raise
        logger.error(f"[OpenClaw] 未知错误: {e}", exc_info=True)
        raise OpenClawError("OpenClaw 调用失败") from e

    if not chunks:
        return "[OpenClaw] (无返回)"

    full_reply = "".join(chunks).strip()

    # 审计用：本地 SQLite 留一份最近一轮的 audit log（不发给 OpenClaw）
    # 多轮对话由 OpenClaw 自己管；这里只留 1 条 user + 1 条 assistant 方便主人看
    audit = [
        {"role": "user", "content": message},
        {"role": "assistant", "content": full_reply},
    ]
    _save_session(user_id, session_key, audit)

    return full_reply


class OpenClawCaller(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config or {})
        self.config = config or {}

        # 从 self.config 读所有配置（_conf_schema.json 定义）
        def _cfg(key, default):
            v = self.config.get(key, default)
            # 兼容 {"value": x, "description": "..."} 格式
            if isinstance(v, dict) and "value" in v:
                return v["value"]
            return v

        # 连接配置（_conf_schema.json 强制要求用户填）
        self.openclaw_url = _cfg("openclaw_url", "")
        self.openclaw_token = _cfg("openclaw_token", "")
        self.openclaw_agent_id = _cfg("openclaw_agent_id", "main")
        self.openclaw_timeout = int(_cfg("openclaw_timeout", 300))
        self.openclaw_verify_ssl = _to_bool(_cfg("openclaw_verify_ssl", True), True)

        # 发给 OpenClaw 的 system message 模板；空则不发送 system message
        self.openclaw_system_prompt = str(_cfg("openclaw_system_prompt", "") or "").strip()

        # 项目名完全动态——不需要配置项
        # LLM 自主传 project 字符串（任何 a-z0-9- 格式都行），形成独立 session
        self._known_projects: set[str] = set()  # 运行时 LLM 用过的项目集合（仅用于 /oc reset 提示）

        # session_key 计数器（/oc reset 用）
        self._session_counters: dict[str, int] = {}

        # 校验：openclaw_url / openclaw_token 不能为空
        if not self.openclaw_url or not self.openclaw_token:
            logger.warning(
                "[openclaw_caller] ⚠️ openclaw_url 或 openclaw_token 未配置！"
                "请在 AstrBot WebUI → 插件管理 → astrbot_plugin_openclaw_caller → 配置 中填写。"
                "插件将无法正常委派任务给 OpenClaw。"
            )

        # 访问控制（access_control 配置项，object + items 结构）
        ac_raw = _cfg("access_control", {})
        if not isinstance(ac_raw, dict):
            ac_raw = {}
        self.whitelist_enabled: bool = _to_bool(ac_raw.get("whitelist_enabled", True), True)
        self.allowed_user_ids: set[str] = {
            str(x).strip() for x in (ac_raw.get("allowed_user_ids") or []) if str(x).strip()
        }
        self.block_when_disabled: bool = _to_bool(ac_raw.get("block_when_disabled", False), False)

        # 同步到模块级缓存，供模块级后台协程和工具函数共享运行时配置
        _module_cfg["whitelist_enabled"] = self.whitelist_enabled
        _module_cfg["allowed_user_ids"] = self.allowed_user_ids
        _module_cfg["block_when_disabled"] = self.block_when_disabled
        _module_cfg["openclaw_url"] = self.openclaw_url
        _module_cfg["openclaw_token"] = self.openclaw_token
        _module_cfg["openclaw_agent_id"] = self.openclaw_agent_id
        _module_cfg["openclaw_timeout"] = self.openclaw_timeout
        _module_cfg["openclaw_verify_ssl"] = self.openclaw_verify_ssl
        _module_cfg["openclaw_system_prompt"] = self.openclaw_system_prompt
        _init_db()

        # 内存任务跟踪：task_id -> task_info（用于 /oc pages 显示当前/历史任务）
        self._bg_tasks: dict[str, dict] = {}
        # 模块级同步一份，供模块级 _background_run / cancel API 共享
        _module_cfg["bg_tasks"] = self._bg_tasks
        _module_cfg["task_handles"] = {}

        # 注册 Plugin Page 后端 API
        try:
            context.register_web_api(
                f"/{PLUGIN_NAME}/tasks",
                self._api_list_tasks,
                ["GET"],
                "List OpenClaw tasks (running + history)",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/cancel",
                self._api_cancel_task,
                ["POST"],
                "Cancel a running background task",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/delete",
                self._api_delete_task,
                ["POST"],
                "Delete an OpenClaw task",
            )
        except Exception as e:
            logger.warning(f"[openclaw_caller] register_web_api 失败: {e}")

        logger.info(
            f"[openclaw_caller] 初始化完成: url_configured={bool(self.openclaw_url)}, "
            f"agent_id={self.openclaw_agent_id}, "
            f"has_openclaw_system_prompt={bool(self.openclaw_system_prompt)}"
        )

        # 就绪性检查：未填配置时给主人清晰的提示
        if not self.openclaw_url or not self.openclaw_token:
            ready_msg = (
                "\n\n[astrbot_plugin_openclaw_caller] 配置缺失提示\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "请在 AstrBot WebUI → 插件管理 → astrbot_plugin_openclaw_caller → 配置 中填写：\n"
                "  • openclaw_url      OpenClaw Gateway 根 URL（不要带 /v1/chat/completions）\n"
                "  • openclaw_token    Bearer Token\n"
                "  • openclaw_agent_id  Agent ID（默认 main）\n\n"
                "填写后请重启 AstrBot。\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            )
            logger.warning(ready_msg)

    def _event_platform_key(self, event: AstrMessageEvent | None) -> str:
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
        import re as _re
        return _re.sub(r"[^a-zA-Z0-9_-]+", "-", platform).strip("-").lower() or "unknown"

    def _session_key_for(self, sender_id: str, mode: str = "oc", event: AstrMessageEvent | None = None) -> str:
        """每个用户一个 session 隔离上下文

        /oc reset 调 _reset_session_for 换 session_id，让 OpenClaw 端开新 session
        """
        base = f"{self._event_platform_key(event)}-{sender_id}-{mode}{_system_prompt_session_suffix()}"
        counter = self._session_counters.get(base, 0)
        if counter > 0:
            return f"{base}-r{counter}"
        return base

    def _next_session_key(self, sender_id: str, mode: str = "oc", event: AstrMessageEvent | None = None) -> str:
        """生成下一个 session_key（/oc reset 用）"""
        base = f"{self._event_platform_key(event)}-{sender_id}-{mode}{_system_prompt_session_suffix()}"
        counter = self._session_counters.get(base, 0) + 1
        self._session_counters[base] = counter
        return f"{base}-r{counter}"

    def _normalize_project(self, project: str) -> str:
        """归一化项目名（**完全动态**——不限制清单）

        规则：
        - 空 → fallback "general"
        - 合法格式（a-z 开头 + a-z0-9-_+ 1-31 字符）→ 接受
        - 其它格式 → fallback "general"

        **LLM 自主传任何字符串都行**——任何项目名都形成独立 session。
        """
        p = (project or "").strip().lower()
        if not p:
            return "general"
        # 合法格式：a-z 开头 1-31 字符
        if re.match(r"^[a-z][a-z0-9_\-+]{0,30}$", p):
            return p
        return "general"

    def _parse_oc_prompt(self, prompt: str) -> tuple[str, str]:
        """解析 /oc [project] <任务> 格式（**完全动态**）

        Returns:
            (实际任务文本, 项目名)
        """
        first_word = prompt.strip().split(maxsplit=1)[0].lower()
        # 用 _normalize_project 判断第一个词是否是合法项目名
        # 如果是 → 当作 [project] 前缀剥掉
        if first_word and re.match(r"^[a-z][a-z0-9_\-+]{0,30}$", first_word):
            real_prompt = prompt.strip().split(maxsplit=1)[1] if " " in prompt.strip() else ""
            return real_prompt, first_word
        return prompt, "general"

    @filter.command("oc")
    async def openclaw_command(self, event: AstrMessageEvent, prompt: str = ""):
        """手动调 OpenClaw agent：/oc [project] <任务>（**白名单限制**）

        用法：
            /oc <任务>                       # 默认 general
            /oc <project> <任务>            # 任何 a-z0-9-_+ 字符串都合法
            /oc reset                        # 清空所有项目历史
            /oc reset <project>             # 只清空指定项目
        """
        if not await _check_allowed(self, event):
            return
        if not prompt.strip():
            yield event.plain_result(
                "用法：/oc [project] <任务>\n"
                "  project: **完全动态**——任何 a-z0-9-_+ 字符串都合法\n"
                "  不填 [project] 默认 general\n"
                "  例如: /oc project-a 用 SQL 查最近 7 天数据均值\n"
                "        /oc project-b 写一份调研报告\n"
                "/oc reset [project]    清空历史（不填清全部）\n\n"
                "注意：本插件默认启用白名单，未在 access_control.allowed_user_ids 列表中的用户会被拒绝"
            )
            return

        # 解析可选 [project] 前缀
        prompt, project = self._parse_oc_prompt(prompt)

        # 剥掉 [project] 后 prompt 可能变空（"hello" 这种单 project name）
        # 提示用户补全任务，而不是发空消息给 OpenClaw
        if not prompt.strip():
            yield event.plain_result(
                "提示：你输入的 `{}` 被识别为 project 名。\n".format(project)
                + "请在 project 后补实际任务，例如：\n"
                + "  /oc {} 帮我写一份调研报告\n".format(project)
                + "  /oc {} 用 SQL 查最近 7 天数据\n\n".format(project)
                + "如果不指定 project，直接发：\n"
                + "  /oc 帮我写一份调研报告"
            )
            return

        sender_id = str(event.get_sender_id())
        session_key = self._session_key_for(sender_id, project, event)

        yield event.plain_result(
            f"已委托 OpenClaw agent 跑任务（project={project}）：{prompt[:50]}..."
        )

        # 同步入库（Plugin Page 任务列表可见）
        oc_task_id = f"oc-sync-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        oc_info = {
            "task_id": oc_task_id, "user_id": sender_id, "project": project,
            "task_text": prompt, "status": "running", "mode": "oc-sync",
            "created_at": time.time(), "finished_at": None,
            "result_text": None, "error_text": None,
        }
        _db_insert_task(oc_info)
        try:
            result = await call_openclaw(
                message=prompt,
                session_key=session_key,
                user_id=sender_id,
                project=project,
                openclaw_url=_module_cfg["openclaw_url"],
                openclaw_token=_module_cfg["openclaw_token"],
                openclaw_agent_id=_module_cfg["openclaw_agent_id"],
                openclaw_timeout=_module_cfg["openclaw_timeout"],
                openclaw_verify_ssl=_module_cfg["openclaw_verify_ssl"],
            )
            oc_info["status"] = "done"
            oc_info["finished_at"] = time.time()
            oc_info["result_text"] = result
            _db_update_task(oc_info)
            yield event.plain_result(f"\n{result}")
        except Exception as e:
            oc_info["status"] = "failed"
            oc_info["finished_at"] = time.time()
            # DB 里存完整错误（管理员看）；user 收到脱敏提示（不泄露 URL/Token）
            oc_info["error_text"] = str(e)
            _db_update_task(oc_info)
            yield event.plain_result(f"\n[OpenClaw 调用失败] {_sanitize_error(e)}")

    @filter.command("oc bg")
    async def openclaw_background(self, event: AstrMessageEvent, prompt: str = ""):
        """手动后台调 OpenClaw agent：/oc bg [project] <任务>（**白名单限制**）

        用法：
            /oc bg <任务>                # 后台跑，默认 general
            /oc bg <project> <任务>     # **完全动态**——任何 a-z0-9-_+ 字符串都合法
        """
        if not await _check_allowed(self, event):
            return
        if not prompt.strip():
            yield event.plain_result(
                "用法：/oc bg [project] <任务>\n"
                "  任务会在后台独立跑，跑完会主动通知主人\n"
                "  project 完全动态——任何 a-z0-9-_+ 字符串都合法\n"
                "  例如: /oc bg project-a 用 SQL 查最近 7 天数据均值\n"
                "        /oc bg project-b 跑一个长调研"
            )
            return

        # 解析可选 [project] 前缀
        prompt, project = self._parse_oc_prompt(prompt)
        sender_id = str(event.get_sender_id())
        session_key = self._session_key_for(sender_id, project, event)

        # 启动后台协程
        task_id = f"bg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        task_handle = asyncio.create_task(self._background_run(
            task=prompt,
            session_key=session_key,
            user_id=sender_id,
            project=project,
            task_id=task_id,
            event=event,
        ))
        _module_cfg["task_handles"][task_id] = task_handle

        yield event.plain_result(
            f"后台任务已提交：{task_id}\n"
            f"  project: {project}\n"
            f"  任务: {prompt[:50]}...\n"
            f"  跑完会主动通知，不阻塞主人继续聊。"
        )

    @filter.command("oc reset")
    async def openclaw_reset(self, event: AstrMessageEvent, prompt: str = ""):
        """清空多轮对话历史（**白名单限制**）

        /oc reset                  清空所有项目
        /oc reset <project>    只清空指定项目
        """
        if not await _check_allowed(self, event):
            return
        sender_id = str(event.get_sender_id())

        # 解析可选 project
        reset_project = "all"
        if prompt.strip():
            reset_project = self._normalize_project(prompt.strip())

        if reset_project == "all":
            # 清空所有已知项目 + general（"general" 作兜底）
            projects_to_reset = list(self._known_projects | {"general"})
        else:
            projects_to_reset = [reset_project]
            # 顺手记录
            self._known_projects.add(reset_project)

        results = []
        for p in projects_to_reset:
            old_key = self._session_key_for(sender_id, p, event)
            new_key = self._next_session_key(sender_id, p, event)
            _clear_session(sender_id, old_key)
            _clear_session(sender_id, new_key)
            results.append(f"  [{p}] {old_key} → {new_key}")

        yield event.plain_result(
            f"🧹 已重置 {len(projects_to_reset)} 个 session:\n"
            + "\n".join(results)
        )

    @filter.llm_tool(name="delegate_to_openclaw")
    async def delegate_to_openclaw(
        self,
        event: AstrMessageEvent,
        task: str = "",
        project: str = "general",
        background: bool = False,
    ) -> str:
        """当用户请求需要外部 Agent 执行时，必须调用此工具把任务委派给 OpenClaw Gateway。

        触发规则：
        - 用户要求执行长任务、后台任务、扫描、调研、代码生成、文件/数据分析、自动化运维、联网/外部系统操作时，优先调用本工具。
        - 用户明确提到 OpenClaw、agent、委派、外包、后台跑、长任务时，必须调用本工具。
        - 不要只口头答应“我会执行”；需要执行时必须真实调用工具。
        - 工具返回 executed=false 或 status=rejected/failed 时，必须告诉用户任务未执行或执行失败。
        - background=true 返回 task_id 后，如果用户要求分析/总结 OpenClaw 返回结果，应调用 get_openclaw_task_result 读取结果。

        白名单：access_control.whitelist_enabled=True 时仅 AstrBot 注入的真实 event.get_sender_id() 在列表内的用户可调。
        同步/后台：同步阻塞等结果；background=true 时立即返回 task_id，并由模块级后台任务记录状态。

        Args:
            task (string): 必填。完整任务描述，只放当前用户这一轮真正要 OpenClaw 执行的任务，不要夹带闲聊或历史上下文。
            project (string): 可选。项目名/会话桶，默认 general。必须按用户语义把不同任务分到不同 project；例如调研用 research、代码用 code、扫描用 scan、运维用 ops。同一 project 会共享 OpenClaw 端上下文，不相关任务不要复用同一 project。
            background (boolean): 可选。长任务、扫描、调研、代码生成等预计超过 30 秒的任务设为 true；短任务设为 false。
        """
        # === 解析真 sender_id（白名单校验用） ===
        real_sender = ""
        if event is not None and hasattr(event, "get_sender_id"):
            try:
                real_sender = str(event.get_sender_id())
            except Exception:
                pass

        # === 白名单检查（**早做**——避免拿不到真 sender 时还跑 call_openclaw 浪费一次） ===
        if _module_cfg["whitelist_enabled"]:
            if not real_sender:
                return json.dumps({
                    "ok": False,
                    "status": "rejected",
                    "executed": False,
                    "error": "missing_sender_id",
                    "reply": "任务未执行：无法确认调用者身份，请重新发送任务。",
                }, ensure_ascii=False)
            if real_sender not in _module_cfg["allowed_user_ids"]:
                if not _module_cfg["block_when_disabled"]:
                    return json.dumps({
                        "ok": False,
                        "status": "rejected",
                        "executed": False,
                        "error": "not_in_whitelist",
                        "reply": "任务未执行：你不在 OpenClaw 调用白名单中。",
                    }, ensure_ascii=False)
                return json.dumps({
                    "ok": False,
                    "status": "rejected",
                    "executed": False,
                    "error": "forbidden",
                    "reply": "任务未执行。",
                }, ensure_ascii=False)
            sender_id = real_sender
        else:
            # 白名单关闭：拿不到真 sender 时用 anonymous 占位（不抛错，方便 LLM 自由调用）
            sender_id = real_sender or "anonymous"

        # === 兼容：拿 event 给后续 session_key / 后续代码用 ===
        # 若框架没注入真 event，则构造一个带 sender_id 的轻量 event
        if event is not None and hasattr(event, "get_sender_id"):
            pass  # 已经是真 event
        else:
            # 构造一个"方法齐全"的 LiteEvent mock——只用于 session_key 解析 / 元信息查询
            # 后续 _background_run_module 拿到这个 event 后，会校验是否 LiteEvent；
            # 若是 LiteEvent（即没有真推送通道），任务状态会标 no_recipient 而不是 done
            event = _make_lite_event(sender_id)

        # === 归一化 project ===
        import re as _re
        p = (project or "").strip().lower()
        if not p or not _re.match(r"^[a-z][a-z0-9_\-+]{0,30}$", p):
            project = "general"

        # sender_id 只来自 AstrBot 注入的 event，不信任 LLM 传参
        session_key = self._session_key_for(sender_id, project, event)

        # 后台模式：调模块级 _background_run（不阻塞 LLM）
        if background:
            task_id = f"bg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
            task_handle = asyncio.create_task(_background_run_module(
                task=task,
                session_key=session_key,
                user_id=sender_id,
                project=project,
                task_id=task_id,
                event=event,
            ))
            _module_cfg["task_handles"][task_id] = task_handle
            return json.dumps({
                "ok": True,
                "status": "submitted",
                "executed": True,
                "task_id": task_id,
                "project": project,
                "message": f"任务已提交后台运行（project={project}），跑完后会主动通知主人",
            }, ensure_ascii=False)

        # 同步模式：写库审计（Plugin Page 任务列表可见）
        task_id = f"tool-sync-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        info = {
            "task_id": task_id, "user_id": sender_id, "project": project,
            "task_text": task, "status": "running", "mode": "tool-sync",
            "created_at": time.time(), "finished_at": None,
            "result_text": None, "error_text": None,
        }
        _db_insert_task(info)
        try:
            result = await call_openclaw(
                message=task,
                session_key=session_key,
                user_id=sender_id,
                project=project,
                openclaw_url=_module_cfg["openclaw_url"],
                openclaw_token=_module_cfg["openclaw_token"],
                openclaw_agent_id=_module_cfg["openclaw_agent_id"],
                openclaw_timeout=_module_cfg["openclaw_timeout"],
                openclaw_verify_ssl=_module_cfg["openclaw_verify_ssl"],
            )
            info["status"] = "done"
            info["finished_at"] = time.time()
            info["result_text"] = result
            _db_update_task(info)
            return json.dumps({
                "ok": True,
                "status": "done",
                "executed": True,
                "reply": result,
                "project": project,
            }, ensure_ascii=False)
        except Exception as e:
            info["status"] = "failed"
            info["finished_at"] = time.time()
            info["error_text"] = str(e)
            _db_update_task(info)
            return json.dumps({
                "ok": False,
                "status": "failed",
                "executed": False,
                "error": _sanitize_error(e),
                "reply": "任务执行失败，详情见 AstrBot 日志。",
            }, ensure_ascii=False)

    @filter.llm_tool(name="get_openclaw_task_result")
    async def get_openclaw_task_result(
        self,
        event: AstrMessageEvent,
        task_id: str = "",
        project: str = "",
        max_chars: int = 12000,
    ) -> str:
        """读取 OpenClaw 任务结果，供 AstrBot 主 LLM 分析、总结或继续调度。

        触发规则：
        - 用户要求分析、总结、解释、归纳、复盘 OpenClaw 返回结果时，必须调用本工具读取结果。
        - 用户提到 task_id 时，按 task_id 读取；未提供 task_id 时，读取当前用户最近一个任务结果。
        - 如果任务还在 running，应告诉用户结果尚未完成，不要编造结果。

        Args:
            task_id (string): 可选。要读取的 OpenClaw 任务 ID；留空则读取最近一个任务。
            project (string): 可选。按 project 过滤最近任务，如 research、code、scan、ops。
            max_chars (number): 可选。最多返回多少字符，默认 12000，最大 50000。
        """
        sender_id = ""
        if event is not None and hasattr(event, "get_sender_id"):
            try:
                sender_id = str(event.get_sender_id())
            except Exception:
                sender_id = ""
        if not sender_id:
            return json.dumps({
                "ok": False,
                "error": "missing_sender_id",
                "reply": "无法确认调用者身份，不能读取任务结果。",
            }, ensure_ascii=False)
        if _module_cfg["whitelist_enabled"] and sender_id not in _module_cfg["allowed_user_ids"]:
            return json.dumps({
                "ok": False,
                "error": "forbidden",
                "reply": "你不在 OpenClaw 调用白名单中，不能读取任务结果。",
            }, ensure_ascii=False)
        try:
            limit = int(max_chars or 12000)
        except (TypeError, ValueError):
            limit = 12000
        limit = max(1000, min(limit, 50000))
        row = _db_get_task_for_user(sender_id, (task_id or "").strip(), (project or "").strip())
        if not row:
            return json.dumps({
                "ok": False,
                "error": "task_not_found",
                "reply": "未找到可读取的 OpenClaw 任务结果。",
            }, ensure_ascii=False)
        text = row.get("result_text") or row.get("error_text") or ""
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]
        return json.dumps({
            "ok": True,
            "task_id": row.get("task_id"),
            "project": row.get("project"),
            "status": row.get("status"),
            "mode": row.get("mode"),
            "task_text": row.get("task_text"),
            "created_at": row.get("created_at"),
            "finished_at": row.get("finished_at"),
            "truncated": truncated,
            "max_chars": limit,
            "result": text,
            "reply": text if text else "任务尚未产生结果。",
        }, ensure_ascii=False)

    async def _background_run(
        self,
        task: str,
        session_key: str,
        user_id: str,
        project: str,
        task_id: str,
        event: AstrMessageEvent,
    ):
        """后台跑 call_openclaw，跑完用 event.send 通知主人

        同时把任务生命周期写入 SQLite（openclaw_tasks）和模块级 _module_cfg["bg_tasks"]，
        供 /oc pages 显示用。
        """
        created_at = time.time()
        info = {
            "task_id": task_id, "user_id": user_id, "project": project,
            "task_text": task, "status": "running", "mode": "background",
            "created_at": created_at, "finished_at": None, "result_text": None, "error_text": None,
        }
        _module_cfg["bg_tasks"][task_id] = info
        _db_insert_task(info)
        try:
            result = await call_openclaw(
                message=task,
                session_key=session_key,
                user_id=user_id,
                project=project,
                openclaw_url=_module_cfg["openclaw_url"],
                openclaw_token=_module_cfg["openclaw_token"],
                openclaw_agent_id=_module_cfg["openclaw_agent_id"],
                openclaw_timeout=_module_cfg["openclaw_timeout"],
                openclaw_verify_ssl=_module_cfg["openclaw_verify_ssl"],
            )
            if info.get("status") == "cancelled":
                return
            # 跑完了：更新状态 + 推 SQLite
            info["status"] = "done"
            info["finished_at"] = time.time()
            info["result_text"] = result
            _db_update_task(info)
            msg = (
                f"✅ 后台任务 {task_id} 完成（project={project}）\n\n"
                f"{result}"
            )
            await event.send(MessageChain([Plain(msg)]))
        except asyncio.CancelledError:
            info["status"] = "cancelled"
            info["finished_at"] = time.time()
            _db_update_task(info)
            logger.info(f"后台任务 {task_id} 已取消")
        except Exception as e:
            info["status"] = "failed"
            info["finished_at"] = time.time()
            info["error_text"] = str(e)
            _db_update_task(info)
            # 日志留全；user 收到脱敏
            err = f"❌ 后台任务 {task_id} 失败：{_sanitize_error(e)}"
            logger.error(f"❌ 后台任务 {task_id} 失败：{e}", exc_info=True)
            try:
                await event.send(MessageChain([Plain(err)]))
            except Exception as send_err:
                logger.error(f"event.send 也失败：{send_err}")
        finally:
            # 当前内存记录保留 1 小时供页面查询
            async def _gc():
                await asyncio.sleep(3600)
                _module_cfg["bg_tasks"].pop(task_id, None)
                _module_cfg["task_handles"].pop(task_id, None)
            asyncio.create_task(_gc())

    # --- Page API handlers（Plugin Page 后端路由） ---
    async def _api_list_tasks(self):
        """GET /api/plug/astrbot_plugin_openclaw_caller/tasks"""
        try:
            _init_db()
            with sqlite3.connect(_get_db_path()) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT task_id,project,task_text,status,mode,created_at,finished_at "
                    "FROM openclaw_tasks ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
            tasks = [dict(r) for r in rows]
            return jsonify({
                "ok": True,
                "tasks": tasks,
                "running_count": sum(1 for t in tasks if t["status"] == "running"),
                "total_count": len(tasks),
            })
        except Exception as e:
            # WebUI 拿脱敏错误，完整 stack 走 logger（admin 看）
            logger.error(f"_api_list_tasks 异常：{e}", exc_info=True)
            return jsonify({"ok": False, "error": _sanitize_error(e), "tasks": []}), 500

    async def _api_cancel_task(self):
        """POST /api/plug/astrbot_plugin_openclaw_caller/cancel body: {"task_id": "bg-..."}"""
        from quart import request
        try:
            data = await request.get_json() or {}
            task_id = data.get("task_id", "").strip()
            if not task_id:
                return jsonify({"ok": False, "error": "task_id required"}), 400
            info = _module_cfg["bg_tasks"].get(task_id)
            if not info:
                return jsonify({"ok": False, "error": "task not found or already finished"}), 404
            handle = _module_cfg["task_handles"].get(task_id)
            info["status"] = "cancelled"
            info["finished_at"] = time.time()
            _db_update_task(info)
            if handle and not handle.done():
                handle.cancel()
            return jsonify({"ok": True, "task_id": task_id, "status": "cancelled"})
        except Exception as e:
            logger.error(f"_api_cancel_task 异常：{e}", exc_info=True)
            return jsonify({"ok": False, "error": _sanitize_error(e)}), 500

    async def _api_delete_task(self):
        """POST /api/plug/astrbot_plugin_openclaw_caller/delete body: {"task_id": "..."}"""
        from quart import request
        try:
            data = await request.get_json() or {}
            task_id = data.get("task_id", "").strip()
            if not task_id:
                return jsonify({"ok": False, "error": "task_id required"}), 400
            info = _module_cfg["bg_tasks"].pop(task_id, None)
            handle = _module_cfg["task_handles"].pop(task_id, None)
            if handle and not handle.done():
                handle.cancel()
            deleted = _db_delete_task(task_id)
            if not deleted and not info:
                return jsonify({"ok": False, "error": "task not found"}), 404
            return jsonify({"ok": True, "task_id": task_id, "deleted": True})
        except Exception as e:
            logger.error(f"_api_delete_task 异常：{e}", exc_info=True)
            return jsonify({"ok": False, "error": _sanitize_error(e)}), 500


async def _background_run_module(
    task: str,
    session_key: str,
    user_id: str,
    project: str,
    task_id: str,
    event,  # 可能是真 AstrMessageEvent，也可能是 _make_lite_event 构造的 mock
):
    """模块级后台跑（delegate_to_openclaw Tool 用，不阻塞 LLM）

    与类内 _background_run 逻辑完全一致——但走模块级 + 读 _module_cfg。
    写入同一份 _module_cfg["bg_tasks"]，所以 /oc pages + 取消 API 都能看到。

    **event 必须由 caller 显式传进来**——不接受全局 event 缓存，避免跨用户竞态。
    """
    created_at = time.time()
    info = {
        "task_id": task_id, "user_id": user_id, "project": project,
        "task_text": task, "status": "running", "mode": "background",
        "created_at": created_at, "finished_at": None, "result_text": None, "error_text": None,
    }
    _module_cfg["bg_tasks"][task_id] = info
    _db_insert_task(info)
    # 用 caller 显式传的 event——避免全局缓存的跨用户竞态
    if event is not None and not getattr(event, "_is_lite", False):
        real_event = event
        has_recipient = True
    else:
        # event 是 LiteEvent mock（send no-op，不能真推送）——任务标 no_recipient
        real_event = event
        has_recipient = False
        logger.warning(
            f"[{task_id}] 后台任务起跑但 caller 传来的 event 是 LiteEvent mock"
            f"（框架没把真 AstrMessageEvent 注入到 Tool args）。结果仅写 SQLite，Plugin Page 标红。"
        )
    try:
        result = await call_openclaw(
            message=task,
            session_key=session_key,
            user_id=user_id,
            project=project,
            openclaw_url=_module_cfg["openclaw_url"],
            openclaw_token=_module_cfg["openclaw_token"],
            openclaw_agent_id=_module_cfg["openclaw_agent_id"],
            openclaw_timeout=_module_cfg["openclaw_timeout"],
            openclaw_verify_ssl=_module_cfg["openclaw_verify_ssl"],
        )
        if info.get("status") == "cancelled":
            return
        info["finished_at"] = time.time()
        info["result_text"] = result
        if has_recipient:
            info["status"] = "done"
            _db_update_task(info)
            msg = (
                f"✅ 后台任务 {task_id} 完成（project={project}）\n\n"
                f"{result}"
            )
            try:
                await real_event.send(MessageChain([Plain(msg)]))
            except Exception as send_err:
                # 真 event 也不可用（platform 适配器关 / ws 断）——降级为 no_recipient
                logger.error(f"[{task_id}] 真 event.send 失败，降级 no_recipient: {send_err}")
                info["status"] = "no_recipient"
                _db_update_task(info)
        else:
            info["status"] = "no_recipient"
            _db_update_task(info)
            logger.warning(
                f"[{task_id}] 任务完成但无推送通道。结果仅写 SQLite。"
            )
    except asyncio.CancelledError:
        info["status"] = "cancelled"
        info["finished_at"] = time.time()
        _db_update_task(info)
        logger.info(f"[{task_id}] 后台任务已取消")
    except Exception as e:
        info["finished_at"] = time.time()
        info["error_text"] = str(e)
        info["status"] = "failed"
        _db_update_task(info)
        # 日志留全；user 收到脱敏
        err = f"❌ 后台任务 {task_id} 失败：{_sanitize_error(e)}"
        logger.error(f"❌ 后台任务 {task_id} 失败：{e}", exc_info=True)
        if has_recipient:
            try:
                await real_event.send(MessageChain([Plain(err)]))
            except Exception as send_err:
                logger.error(f"[{task_id}] 失败消息推送也失败: {send_err}")
    finally:
        # 内存记录保留 1 小时供页面查询
        async def _gc():
            await asyncio.sleep(3600)
            _module_cfg["bg_tasks"].pop(task_id, None)
            _module_cfg["task_handles"].pop(task_id, None)
        asyncio.create_task(_gc())


def _db_insert_task(info: dict):
    """把任务信息写入 SQLite 审计表（模块级——所有路径都能调）"""
    try:
        _init_db()
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO openclaw_tasks "
                "(task_id,user_id,project,task_text,status,mode,created_at,finished_at,result_text,error_text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (info["task_id"], info["user_id"], info["project"], info["task_text"],
                 info["status"], info["mode"], info["created_at"], info["finished_at"],
                 info.get("result_text"), info.get("error_text")),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"[openclaw_caller] _db_insert_task 失败: {e}")


def _db_update_task(info: dict):
    """更新 SQLite 任务审计表（模块级）"""
    try:
        _init_db()
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute(
                "UPDATE openclaw_tasks SET status=?, finished_at=?, result_text=?, error_text=? WHERE task_id=?",
                (info["status"], info["finished_at"], info.get("result_text"),
                 info.get("error_text"), info["task_id"]),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"[openclaw_caller] _db_update_task 失败: {e}")


def _db_get_task_for_user(user_id: str, task_id: str = "", project: str = "") -> dict | None:
    try:
        _init_db()
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            if task_id:
                row = conn.execute(
                    "SELECT task_id,user_id,project,task_text,status,mode,created_at,finished_at,result_text,error_text "
                    "FROM openclaw_tasks WHERE user_id=? AND task_id=?",
                    (user_id, task_id),
                ).fetchone()
            elif project:
                row = conn.execute(
                    "SELECT task_id,user_id,project,task_text,status,mode,created_at,finished_at,result_text,error_text "
                    "FROM openclaw_tasks WHERE user_id=? AND project=? ORDER BY created_at DESC LIMIT 1",
                    (user_id, project),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT task_id,user_id,project,task_text,status,mode,created_at,finished_at,result_text,error_text "
                    "FROM openclaw_tasks WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.warning(f"[openclaw_caller] _db_get_task_for_user 失败: {e}")
        return None


def _db_delete_task(task_id: str) -> bool:
    try:
        _init_db()
        with sqlite3.connect(_get_db_path()) as conn:
            cur = conn.execute("DELETE FROM openclaw_tasks WHERE task_id=?", (task_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        logger.warning(f"[openclaw_caller] _db_delete_task 失败: {e}")
        return False
