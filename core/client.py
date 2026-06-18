"""OpenClawClient——封装 /v1/chat/completions 的流式调用。

行为与原 ``call_openclaw`` 模块级函数**完全一致**：
- 同样的 SSE 解析、错误处理、首轮双保险
- system prompt 模板在 session_key 首次开始时注入
- 首轮同时发 ``messages[0].role=system`` 和把模板内嵌到首条 user 消息开头
- 错误 / 超时时回滚 ``_initialized_sessions`` 标记
- 完整审计：本地 SQLite 留最近 1 轮 user+assistant（不发 OpenClaw）

区别：
- 不再依赖 ``_module_cfg`` 全局字典
- config / _initialized_sessions 都由实例持有
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import aiohttp
from astrbot.api import logger

from .session import render_openclaw_system_prompt
from .storage import load_session, save_session
from .util import OpenClawError, digest, new_request_id


class OpenClawClient:
    """OpenClaw Gateway 客户端。"""

    def __init__(
        self,
        url: str,
        token: str,
        agent_id: str,
        timeout: int,
        verify_ssl: bool,
        system_prompt_template: str = "",
    ):
        self.url = url
        self.token = token
        self.agent_id = agent_id
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.system_prompt_template = system_prompt_template
        # 跟踪哪些 session_key 已经完成首轮 system prompt 注入
        # 错误 / 超时会回滚（discard），确保下次重试时能再次注入
        self._initialized_sessions: set[str] = set()

    def discard_initialized_session(self, session_key: str) -> None:
        """/oc reset 用：清掉首轮注入标记，让该 session 重新走首轮流程。"""
        self._initialized_sessions.discard(session_key)

    async def call(
        self,
        message: str,
        session_key: str,
        user_id: str,
        project: str,
    ) -> str:
        """调 OpenClaw Gateway /v1/chat/completions（OpenAI 协议，多轮对话）。

        行为与原 ``call_openclaw`` 模块级函数 1:1 一致（仅参数来源从 _module_cfg 改为 self）。
        """
        request_id = new_request_id()
        url = self.url
        token = self.token
        agent_id = self.agent_id or "main"
        timeout_seconds = self.timeout or 300
        verify_ssl = (
            self.verify_ssl
            if isinstance(self.verify_ssl, bool)
            else True
        )

        if url and not url.rstrip("/").endswith("/chat/completions"):
            url = url.rstrip("/") + "/v1/chat/completions"

        # 构造 messages：只发当前 user message；system prompt 仅在 session_key 首次开始时发送一次。
        # 首轮采用 system role + user 首部内嵌双保险：有些 OpenAI 兼容 Gateway 不会把 system role 写入长期 session。
        # OpenClaw 自己按 user 字段维护多轮 session，桥接层不重发历史，避免上下文重复注入 + 隐私泄漏。
        messages: list[dict] = []
        audit_messages = load_session(user_id, session_key)
        is_session_started = (
            bool(audit_messages) or session_key in self._initialized_sessions
        )
        _sp = render_openclaw_system_prompt(
            self.system_prompt_template, project, user_id, session_key
        )
        just_initialized_session = False
        if _sp and not is_session_started:
            messages.append({"role": "system", "content": _sp})
            self._initialized_sessions.add(session_key)
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
        sender_digest = digest(user_id)
        session_digest = digest(session_key)
        logger.info(
            f"[OpenClaw call] phase=start request_id={request_id} "
            f"project={project} sender={sender_digest} session={session_digest} "
            f"task_chars={len(message)} has_sp={bool(_sp)} "
            f"sp_injected={just_initialized_session}"
        )

        payload = {
            "model": f"openclaw:{agent_id}",
            "messages": messages,
            "user": session_key,
            "stream": True,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

        chunks: list[str] = []
        first_chunk_time: Optional[float] = None
        t0 = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            if verify_ssl:
                connector = aiohttp.TCPConnector()
            else:
                logger.warning(
                    "[OpenClaw] SSL 证书校验已关闭（_conf_schema.json.openclaw_verify_ssl=false）"
                    "——Bearer Token 将以明文在网络中传输，仅限本地/自签名证书场景"
                )
                connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"[OpenClaw HTTP {resp.status}] {body[:1000]}")
                        raise OpenClawError(f"OpenClaw HTTP {resp.status}")
                    content_type = resp.headers.get("Content-Type", "")
                    if content_type.startswith("text/event-stream"):
                        buf = b""
                        SSE_BUF_MAX = 1_048_576  # 1 MB——防 Gateway bug 把内存撑爆
                        done = False
                        async for raw_line in resp.content:
                            buf += raw_line
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
                                        if (
                                            isinstance(evt, dict)
                                            and "choices" in evt
                                        ):
                                            delta = evt["choices"][0].get("delta", {})
                                            content = delta.get("content", "")
                                            if content:
                                                if first_chunk_time is None:
                                                    first_chunk_time = time.time() - t0
                                                    logger.info(
                                                        f"[OpenClaw call] phase=stream "
                                                        f"request_id={request_id} "
                                                        f"first_chunk_s={first_chunk_time:.2f}"
                                                    )
                                                chunks.append(content)
                                    except json.JSONDecodeError:
                                        pass
                            if done:
                                break
                    else:
                        body = await resp.text()
                        try:
                            obj = json.loads(body)
                            if isinstance(obj, dict) and "choices" in obj:
                                chunks.append(obj["choices"][0]["message"]["content"])
                        except Exception:
                            chunks.append(body[:1000])
        except asyncio.TimeoutError:
            if just_initialized_session:
                self._initialized_sessions.discard(session_key)
            # 已收到部分内容 → 优雅降级：返回部分响应 + 提示，不当失败
            if chunks:
                partial = "".join(chunks).strip()
                logger.warning(
                    f"[OpenClaw call] phase=end request_id={request_id} "
                    f"status=partial_response timeout_s={timeout_seconds} "
                    f"chunks={len(chunks)} response_chars={len(partial)} "
                    f"total_s={time.time() - t0:.2f}（超时，但已收到部分内容）"
                )
                return f"{partial}\n\n[⚠️ OpenClaw 响应超时（{timeout_seconds}s），以上为已收到的部分内容]"
            logger.error(
                f"[OpenClaw call] phase=end request_id={request_id} "
                f"status=timeout timeout_s={timeout_seconds} "
                f"total_s={time.time() - t0:.2f}"
            )
            raise OpenClawError(f"OpenClaw 请求超时（{timeout_seconds}s）")
        except aiohttp.ClientError as e:
            # 已收到部分内容 → 优雅降级：OpenClaw 推了一段后连接中断，常见原因：
            # - Gateway OOM / panic / 重启
            # - 反向代理 idle timeout（如 nginx 60s）掐掉了 chunked 长连接
            # - 服务端有内部超时，主动掐掉 LLM 流
            if chunks:
                partial = "".join(chunks).strip()
                logger.warning(
                    f"[OpenClaw call] phase=end request_id={request_id} "
                    f"status=partial_response chunks={len(chunks)} "
                    f"response_chars={len(partial)} "
                    f"error={type(e).__name__} "
                    f"total_s={time.time() - t0:.2f}（连接中断，但已收到部分内容）"
                )
                return f"{partial}\n\n[⚠️ OpenClaw 连接中断，响应可能不完整（{type(e).__name__}）]"
            if just_initialized_session:
                self._initialized_sessions.discard(session_key)
            logger.error(
                f"[OpenClaw call] phase=end request_id={request_id} "
                f"status=connection_error error={type(e).__name__} "
                f"total_s={time.time() - t0:.2f}",
                exc_info=True,
            )
            raise OpenClawError("OpenClaw 连接错误") from e
        except Exception as e:
            if just_initialized_session:
                self._initialized_sessions.discard(session_key)
            if isinstance(e, OpenClawError):
                # 上层 HTTP / 超时分支已各自 log——这里不重复
                raise
            logger.error(
                f"[OpenClaw call] phase=end request_id={request_id} "
                f"status=unknown_error error={type(e).__name__} "
                f"total_s={time.time() - t0:.2f}",
                exc_info=True,
            )
            raise OpenClawError("OpenClaw 调用失败") from e

        if not chunks:
            logger.warning(
                f"[OpenClaw call] phase=end request_id={request_id} "
                f"status=empty_response chunks=0 "
                f"total_s={time.time() - t0:.2f}"
            )
            return "[OpenClaw] (无返回)"

        full_reply = "".join(chunks).strip()

        logger.info(
            f"[OpenClaw call] phase=end request_id={request_id} "
            f"status=ok chunks={len(chunks)} "
            f"first_chunk_s={first_chunk_time if first_chunk_time is not None else 0:.2f} "
            f"total_s={time.time() - t0:.2f} "
            f"response_chars={len(full_reply)}"
        )

        # 审计用：本地 SQLite 留最近一轮 user+assistant（不发给 OpenClaw）
        audit = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": full_reply},
        ]
        save_session(user_id, session_key, audit)

        return full_reply
