"""astrbot_plugin_openclaw_caller — Star 入口（薄）

把 main.py 的所有非-Star 逻辑都拆到 ``core/`` 子包，本文件只保留：
- OpenClawCaller(Star) 子类
- filter 命令（@filter.command）：/oc、/oc bg、/oc reset
- LLM Tool（@filter.llm_tool）：delegate_to_openclaw、get_openclaw_task_result
- Web API handler 包装（core/api.py 的薄方法包装，给 register_web_api 用）

所有运行时状态（task_handles、initialized_sessions、bg_tasks、session_counters 等）
**全部收进 self 实例字段**，不再有 module-level 可变字典。

行为与重构前 **1:1 一致**——纯结构拆分 + 状态封装。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

from .core.access import check_allowed
from .core.api import api_cancel_task, api_delete_task, api_list_tasks
from .core.client import OpenClawClient
from .core.lite_event import make_lite_event
from .core.runner import background_run
from .core.session import (
    event_platform_key,
    extract_send_target,
    make_next_session_key,
    make_session_key,
    normalize_project,
    parse_oc_prompt,
    system_prompt_session_suffix,
)
from .core.storage import TaskLog, clear_session, init_db
from .core.util import PLUGIN_NAME, digest, sanitize_error, to_bool


class OpenClawCaller(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config or {})
        self.config = config or {}

        def _cfg(key, default):
            v = self.config.get(key, default)
            if isinstance(v, dict) and "value" in v:
                return v["value"]
            return v

        # === 连接配置 ===
        self.openclaw_url = _cfg("openclaw_url", "")
        self.openclaw_token = _cfg("openclaw_token", "")
        self.openclaw_agent_id = _cfg("openclaw_agent_id", "main")
        self.openclaw_timeout = int(_cfg("openclaw_timeout", 1800))
        self.openclaw_verify_ssl = to_bool(_cfg("openclaw_verify_ssl", True), True)

        # 发给 OpenClaw 的 system message 模板；空则不发送 system message
        self.openclaw_system_prompt = str(_cfg("openclaw_system_prompt", "") or "").strip()

        # === 访问控制 ===
        ac_raw = _cfg("access_control", {})
        if not isinstance(ac_raw, dict):
            ac_raw = {}
        self.whitelist_enabled: bool = to_bool(ac_raw.get("whitelist_enabled", True), True)
        self.allowed_user_ids: set[str] = {
            str(x).strip() for x in (ac_raw.get("allowed_user_ids") or []) if str(x).strip()
        }
        self.block_when_disabled: bool = to_bool(ac_raw.get("block_when_disabled", False), False)

        # === 运行时状态（**全部在 self 上，无 module-level mutable**） ===
        # 客户端：封装备 /v1/chat/completions
        self._client = OpenClawClient(
            url=self.openclaw_url,
            token=self.openclaw_token,
            agent_id=self.openclaw_agent_id,
            timeout=self.openclaw_timeout,
            verify_ssl=self.openclaw_verify_ssl,
            system_prompt_template=self.openclaw_system_prompt,
        )
        # 任务审计 SQLite
        self._task_log = TaskLog()
        # 内存任务跟踪：task_id -> task_info（Plugin Page 显示用）
        self._bg_tasks: dict[str, dict] = {}
        # asyncio Task 句柄（cancel API 用）
        self._task_handles: dict[str, asyncio.Task] = {}
        # session_key 计数器（/oc reset 用）
        self._session_counters: dict[str, int] = {}
        # 运行时 LLM 用过的项目集合（仅用于 /oc reset 提示）
        self._known_projects: set[str] = set()

        # === 校验：openclaw_url / openclaw_token 不能为空 ===
        if not self.openclaw_url or not self.openclaw_token:
            logger.warning(
                "[openclaw_caller] ⚠️ openclaw_url 或 openclaw_token 未配置！"
                "请在 AstrBot WebUI → 插件管理 → astrbot_plugin_openclaw_caller → 配置 中填写。"
                "插件将无法正常委派任务给 OpenClaw。"
            )

        # === 初始化 DB ===
        init_db()

        # === 注册 Plugin Page 后端 API ===
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

        # === 就绪性检查：未填配置时给主人清晰的提示 ===
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

    # === 内部 helper：白名单 / session_key / 项目名 / prompt 解析 ===

    async def _check_allowed(self, event: AstrMessageEvent) -> bool:
        return await check_allowed(
            self.whitelist_enabled,
            self.allowed_user_ids,
            self.block_when_disabled,
            event,
        )

    def _event_platform_key(self, event) -> str:
        return event_platform_key(event)

    def _session_key_for(
        self, sender_id: str, mode: str = "oc", event: AstrMessageEvent | None = None
    ) -> str:
        sp_suffix = system_prompt_session_suffix(self.openclaw_system_prompt)
        return make_session_key(
            self._event_platform_key(event),
            sender_id,
            mode,
            sp_suffix,
            self._session_counters,
        )

    def _next_session_key(
        self, sender_id: str, mode: str = "oc", event: AstrMessageEvent | None = None
    ) -> str:
        sp_suffix = system_prompt_session_suffix(self.openclaw_system_prompt)
        return make_next_session_key(
            self._event_platform_key(event),
            sender_id,
            mode,
            sp_suffix,
            self._session_counters,
        )

    async def _tool_sync_mode_for(self, event: AstrMessageEvent | None) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        cid = ""
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if umo and conv_mgr is not None:
            try:
                cid = str(await conv_mgr.get_curr_conversation_id(umo) or "")
            except Exception as e:
                logger.warning(
                    f"[openclaw_caller] 获取 AstrBot 当前 conversation_id 失败: {type(e).__name__}"
                )
        if umo and cid:
            return f"tool-sync-{digest(umo)}-{digest(cid)}"
        if umo:
            return f"tool-sync-{digest(umo)}"
        try:
            session_id = str(getattr(event, "get_session_id", lambda: "")() or "")
        except Exception:
            session_id = ""
        if session_id:
            return f"tool-sync-{digest(session_id)}"
        return "tool-sync"

    def _normalize_project(self, project: str) -> str:
        return normalize_project(project)

    def _parse_oc_prompt(self, prompt: str) -> tuple[str, str]:
        return parse_oc_prompt(prompt)

    # === filter.command: /oc /oc bg /oc reset ===

    @filter.command("oc")
    async def openclaw_command(self, event: AstrMessageEvent, prompt: str = ""):
        """手动调 OpenClaw agent：/oc [project] <任务>（**白名单限制**）

        用法：
            /oc <任务>                       # 默认 general
            /oc <project> <任务>            # 任何 a-z0-9-_+ 字符串都合法
            /oc reset                        # 清空所有项目历史
            /oc reset <project>             # 只清空指定项目
        """
        if not await self._check_allowed(event):
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
        self._known_projects.add(project)
        logger.info(
            f"[OpenClaw cmd] cmd=/oc project={project} "
            f"sender={digest(sender_id)} session={digest(session_key)} "
            f"task_chars={len(prompt)}"
        )

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
        self._task_log.insert(oc_info)
        try:
            result = await self._client.call(
                message=prompt,
                session_key=session_key,
                user_id=sender_id,
                project=project,
            )
            oc_info["status"] = "done"
            oc_info["finished_at"] = time.time()
            oc_info["result_text"] = result
            self._task_log.update(oc_info)
            yield event.plain_result(f"\n{result}")
        except Exception as e:
            oc_info["status"] = "failed"
            oc_info["finished_at"] = time.time()
            # DB 里存完整错误（管理员看）；user 收到脱敏提示（不泄露 URL/Token）
            oc_info["error_text"] = str(e)
            self._task_log.update(oc_info)
            yield event.plain_result(f"\n[OpenClaw 调用失败] {sanitize_error(e)}")

    @filter.command("oc bg")
    async def openclaw_background(self, event: AstrMessageEvent, prompt: str = ""):
        """手动后台调 OpenClaw agent：/oc bg [project] <任务>（**白名单限制**）

        用法：
            /oc bg <任务>                # 后台跑，默认 general
            /oc bg <project> <任务>     # **完全动态**——任何 a-z0-9-_+ 字符串都合法
        """
        if not await self._check_allowed(event):
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
        self._known_projects.add(project)
        logger.info(
            f"[OpenClaw cmd] cmd=/oc_bg project={project} "
            f"sender={digest(sender_id)} session={digest(session_key)} "
            f"task_chars={len(prompt)}"
        )

        # 启动后台协程
        task_id = f"bg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        task_handle = asyncio.create_task(background_run(
            task=prompt,
            session_key=session_key,
            user_id=sender_id,
            project=project,
            task_id=task_id,
            event=event,
            call_openclaw=self._client.call,
            task_log=self._task_log,
            bg_tasks=self._bg_tasks,
            task_handles=self._task_handles,
        ))
        self._task_handles[task_id] = task_handle

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
        if not await self._check_allowed(event):
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
        logger.info(
            f"[OpenClaw cmd] cmd=/oc_reset sender={digest(sender_id)} "
            f"scope={reset_project} projects={len(projects_to_reset)}"
        )

        results = []
        for p in projects_to_reset:
            old_key = self._session_key_for(sender_id, p, event)
            new_key = self._next_session_key(sender_id, p, event)
            clear_session(sender_id, old_key)
            clear_session(sender_id, new_key)
            # 也清掉首轮注入标记——下次 /oc 走新一轮的双保险
            self._client.discard_initialized_session(old_key)
            self._client.discard_initialized_session(new_key)
            results.append(f"  [{p}] {old_key} → {new_key}")

        if reset_project == "all":
            sync_mode = await self._tool_sync_mode_for(event)
            old_key = self._session_key_for(sender_id, sync_mode, event)
            new_key = self._next_session_key(sender_id, sync_mode, event)
            clear_session(sender_id, old_key)
            clear_session(sender_id, new_key)
            self._client.discard_initialized_session(old_key)
            self._client.discard_initialized_session(new_key)
            results.append(f"  [tool-sync] {old_key} → {new_key}")

        yield event.plain_result(
            f"🧹 已重置 {len(results)} 个 session:\n"
            + "\n".join(results)
        )

    # === filter.llm_tool: delegate / get_result ===

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
        - 不要只口头答应"我会执行"；需要执行时必须真实调用工具。
        - 工具返回 executed=false 或 status=rejected/failed 时，必须告诉用户任务未执行或执行失败。
        - background=false 是前台同步模式，可用于和 OpenClaw 进行连续多轮对话：用户要求继续追问、细化、澄清、补充、让 OpenClaw 基于刚才结果继续处理时，应优先用前台同步模式继续沟通。
        - background=true 返回 task_id 后，如果用户要求分析/总结 OpenClaw 返回结果，应调用 get_openclaw_task_result 读取结果。
        - 前台同步沟通若要续接某个后台项目，必须沿用该后台任务的 project；例如后台用 project=research，后续前台追问也传 project=research。

        白名单：access_control.whitelist_enabled=True 时仅 AstrBot 注入的真实 event.get_sender_id() 在列表内的用户可调。
        同步/后台：同步阻塞等结果；background=true 时立即返回 task_id，并由模块级后台任务记录状态。

        Args:
            task (string): 必填。完整任务描述，只放当前用户这一轮真正要 OpenClaw 执行的任务，不要夹带闲聊或历史上下文。
            project (string): 可选。项目名/会话桶，默认 general。传入具体 project 时，同步和后台都会进入同一个 OpenClaw 项目对话；留空/general 时，同步前台会绑定当前 AstrBot 对话，适合普通即时沟通。
            background (boolean): 可选。长任务、扫描、调研、代码生成等预计超过 30 秒的任务设为 true；短任务、追问、澄清、连续多轮沟通设为 false。
        """
        # === 解析真 sender_id（白名单校验用） ===
        real_sender = ""
        if event is not None and hasattr(event, "get_sender_id"):
            try:
                real_sender = str(event.get_sender_id())
            except Exception:
                pass

        # === 白名单检查（**早做**——避免拿不到真 sender 时还跑 call 浪费一次） ===
        if self.whitelist_enabled:
            if not real_sender:
                return json.dumps({
                    "ok": False,
                    "status": "rejected",
                    "executed": False,
                    "error": "missing_sender_id",
                    "reply": "任务未执行：无法确认调用者身份，请重新发送任务。",
                }, ensure_ascii=False)
            if real_sender not in self.allowed_user_ids:
                if not self.block_when_disabled:
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
        if event is not None and hasattr(event, "get_sender_id"):
            pass  # 已经是真 event
        else:
            # 构造一个"方法齐全"的 LiteEvent mock
            event = make_lite_event(sender_id)

        # === 归一化 project ===
        project = self._normalize_project(project)

        project_is_explicit = project != "general"
        if background or project_is_explicit:
            session_mode = project
        else:
            session_mode = await self._tool_sync_mode_for(event)
        session_key = self._session_key_for(sender_id, session_mode, event)
        self._known_projects.add(project)
        logger.info(
            f"[OpenClaw tool] tool=delegate_to_openclaw project={project} "
            f"mode={'background' if background else 'sync'} "
            f"project_explicit={project_is_explicit} "
            f"session_mode={session_mode} "
            f"sender={digest(sender_id)} session={digest(session_key)} "
            f"task_chars={len(task)}"
        )

        # 后台模式：调 background_run（不阻塞 LLM）
        if background:
            task_id = f"bg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
            task_handle = asyncio.create_task(background_run(
                task=task,
                session_key=session_key,
                user_id=sender_id,
                project=project,
                task_id=task_id,
                event=event,
                call_openclaw=self._client.call,
                task_log=self._task_log,
                bg_tasks=self._bg_tasks,
                task_handles=self._task_handles,
                platform_meta=extract_send_target(event),  # v1.2 延迟推送 fallback
                context=self.context,                       # v1.2 延迟推送 fallback
            ))
            self._task_handles[task_id] = task_handle
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
        self._task_log.insert(info)
        try:
            result = await self._client.call(
                message=task,
                session_key=session_key,
                user_id=sender_id,
                project=project,
            )
            info["status"] = "done"
            info["finished_at"] = time.time()
            info["result_text"] = result
            self._task_log.update(info)
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
            self._task_log.update(info)
            return json.dumps({
                "ok": False,
                "status": "failed",
                "executed": False,
                "error": sanitize_error(e),
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
        if self.whitelist_enabled and sender_id not in self.allowed_user_ids:
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
        task_id_q = (task_id or "").strip()
        project_q = (project or "").strip()
        logger.info(
            f"[OpenClaw tool] tool=get_openclaw_task_result sender={digest(sender_id)} "
            f"task_id={task_id_q or '-'} project={project_q or '-'} max_chars={limit}"
        )
        row = self._task_log.get_for_user(sender_id, task_id_q, project_q)
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

    # === Plugin Page Web API 包装（core/api.py 的薄方法包装，给 register_web_api 用） ===

    async def _api_list_tasks(self):
        return await api_list_tasks(self._task_log)

    async def _api_cancel_task(self):
        return await api_cancel_task(self._task_log, self._bg_tasks, self._task_handles)

    async def _api_delete_task(self):
        return await api_delete_task(self._task_log, self._bg_tasks, self._task_handles)
