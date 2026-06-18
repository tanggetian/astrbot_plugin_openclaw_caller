"""后台任务 runner——``/oc bg`` 和 ``delegate_to_openclaw(background=True)`` 都走它。

行为与原模块级 ``_background_run_module`` **完全一致**：
- 把任务生命周期写入 SQLite（openclaw_tasks）和调用方传入的 ``bg_tasks`` 字典
- 识别 LiteEvent mock：真 event 才能推送，否则标 no_recipient
- 异常分类处理：CancelledError / Exception / send 失败
- 1 小时内存 GC

v1.2 新增：
- 推送路径升级——双路径 fallback：
  1. ``event.send()`` 走原路径（多数命令入口仍有效）
  2. 失败 / 无 event 时**自动 fallback** 到 ``context.get_platform().send_message()``
     ——绕开 event 生命周期，是 AstrBot 延迟推送的标准姿势
- 日志增加 ``push_via=event_send | event_send_failed | platform_fallback | platform_fallback_failed``

状态全部从参数传入（不读模块级），方便单测和 reload。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .util import digest


async def _try_send_result(
    event,
    msg: str,
    *,
    task_id: str,
    platform_meta: dict | None,
    context,
    kind: str,  # "done" | "failed" — 仅用于日志区分
) -> bool:
    """按顺序尝试：``event.send()`` → ``context.get_platform().send_message()`` 平台 fallback。

    v1.2 推送路径升级——后台任务完成时按顺序尝试两条路径，确保结果送达用户：

    - **主路径** ``event.send()``：快路径，绝大多数 ``/oc bg`` 命令入口走这条；
      真 event 仍在生命周期内时稳定。
    - **fallback** ``context.get_platform().send_message()``：绕开 event 生命周期，
      是 AstrBot 推送的标准姿势。LiteEvent / event 已 finalize 时走这里。

    任一成功返回 True 并打 ``push_via=event_send | platform_fallback`` 日志；
    都失败返回 False 打 ``push_via=event_send_failed | platform_fallback_failed | all_failed`` 日志。
    """
    # 路径 1（主）：event.send()——对真 event 仍在生命周期内的快任务最直接
    if event is not None and not getattr(event, "_is_lite", False):
        try:
            await event.send(MessageChain([Plain(msg)]))
            logger.info(
                f"[OpenClaw bg] phase=end task_id={task_id} status={kind} push_via=event_send"
            )
            return True
        except Exception as send_err:
            logger.warning(
                f"[OpenClaw bg] phase=end task_id={task_id} status={kind} "
                f"push_via=event_send_failed error={type(send_err).__name__} "
                f"（尝试 platform fallback）"
            )

    # 路径 2（fallback）：context.get_platform().send_message()——不绑 event 生命周期
    if not platform_meta or not context:
        logger.error(
            f"[OpenClaw bg] phase=end task_id={task_id} status={kind} "
            f"push_via=all_failed（event.send 失败且无 platform fallback）"
        )
        return False
    platform_name = platform_meta.get("platform_name", "")
    session_id = platform_meta.get("session_id", "")
    if not platform_name or not session_id:
        logger.error(
            f"[OpenClaw bg] phase=end task_id={task_id} status={kind} "
            f"push_via=all_failed（event.send 失败且 platform_meta 不完整）"
        )
        return False
    try:
        platform = context.get_platform(platform_name)
    except Exception as lookup_err:
        logger.error(
            f"[OpenClaw bg] phase=end task_id={task_id} status={kind} "
            f"push_via=platform_fallback_failed error={type(lookup_err).__name__} "
            f"platform={platform_name}",
            exc_info=True,
        )
        return False
    if platform is None:
        logger.warning(
            f"[OpenClaw bg] phase=end task_id={task_id} status={kind} "
            f"push_via=platform_fallback_failed reason=platform_not_found "
            f"platform={platform_name}"
        )
        return False
    try:
        await platform.send_message(session_id, MessageChain([Plain(msg)]))
        logger.info(
            f"[OpenClaw bg] phase=end task_id={task_id} status={kind} "
            f"push_via=platform_fallback platform={platform_name}"
        )
        return True
    except Exception as fallback_err:
        logger.error(
            f"[OpenClaw bg] phase=end task_id={task_id} status={kind} "
            f"push_via=platform_fallback_failed error={type(fallback_err).__name__} "
            f"platform={platform_name}",
            exc_info=True,
        )
        return False


async def background_run(
    *,
    task: str,
    session_key: str,
    user_id: str,
    project: str,
    task_id: str,
    event,  # 真 AstrMessageEvent 或 _make_lite_event 构造的 mock
    call_openclaw,  # 注入的 OpenClawClient.call（或兼容函数）
    task_log,  # 注入的 TaskLog 实例
    bg_tasks: dict[str, dict[str, Any]],
    task_handles: dict[str, asyncio.Task],
    platform_meta: dict | None = None,  # {platform_name, session_id}——延迟推送 fallback 用
    context=None,  # AstrBot Context——context.get_platform() 拿平台适配器
) -> None:
    """模块级后台跑（delegate_to_openclaw Tool 用，不阻塞 LLM）。

    与原 ``_background_run_module`` 行为 1:1 一致——但走参数注入，
    不读 ``_module_cfg``，方便单测和 reload。

    **event 必须由 caller 显式传进来**——不接受全局 event 缓存，避免跨用户竞态。

    **platform_meta + context 必须由 caller 传进来**——v1.2 引入，用于 event.send 失败时的
    平台适配器 fallback。两者都为 None 时退回 v1.1 行为（event.send 失败就标 no_recipient）。
    """
    created_at = time.time()
    info = {
        "task_id": task_id,
        "user_id": user_id,
        "project": project,
        "task_text": task,
        "status": "running",
        "mode": "background",
        "created_at": created_at,
        "finished_at": None,
        "result_text": None,
        "error_text": None,
    }
    bg_tasks[task_id] = info
    task_log.insert(info)
    sender_digest = digest(user_id)
    session_digest = digest(session_key)
    logger.info(
        f"[OpenClaw bg] phase=start task_id={task_id} project={project} "
        f"sender={sender_digest} session={session_digest} task_chars={len(task)} "
        f"has_platform_fallback={bool(platform_meta and context)}"
    )

    if event is not None and not getattr(event, "_is_lite", False):
        real_event = event
        has_recipient = True
    else:
        real_event = event
        has_recipient = False
        if not (platform_meta and context):
            logger.warning(
                f"[OpenClaw bg] phase=start task_id={task_id} "
                "event_is_lite=true 且无 platform fallback——结果仅写 SQLite，Plugin Page 标 no_recipient。"
            )

    from .util import sanitize_error

    t0 = time.time()
    try:
        result = await call_openclaw(
            message=task,
            session_key=session_key,
            user_id=user_id,
            project=project,
        )
        if info.get("status") == "cancelled":
            logger.info(
                f"[OpenClaw bg] phase=end task_id={task_id} status=cancelled "
                f"total_s={time.time() - t0:.2f}（OpenClaw 返回后被 cancel）"
            )
            return
        info["finished_at"] = time.time()
        info["result_text"] = result
        msg = (
            f"✅ 后台任务 {task_id} 完成（project={project}）\n\n"
            f"{result}"
        )
        if has_recipient or (platform_meta and context):
            info["status"] = "done"
            task_log.update(info)
            sent = await _try_send_result(
                real_event if has_recipient else None,
                msg,
                task_id=task_id,
                platform_meta=platform_meta,
                context=context,
                kind="done",
            )
            if not sent:
                info["status"] = "no_recipient"
                task_log.update(info)
                logger.warning(
                    f"[OpenClaw bg] phase=end task_id={task_id} status=no_recipient "
                    f"total_s={time.time() - t0:.2f} "
                    "（event.send 与 platform fallback 都失败，详见上面 push_via 日志）"
                )
        else:
            info["status"] = "no_recipient"
            task_log.update(info)
            logger.warning(
                f"[OpenClaw bg] phase=end task_id={task_id} status=no_recipient "
                f"reason=event_is_lite_no_fallback total_s={time.time() - t0:.2f} "
                "（任务完成但无任何推送通道）"
            )
    except asyncio.CancelledError:
        info["status"] = "cancelled"
        info["finished_at"] = time.time()
        task_log.update(info)
        logger.info(
            f"[OpenClaw bg] phase=end task_id={task_id} status=cancelled "
            f"total_s={time.time() - t0:.2f}（asyncio 取消）"
        )
    except Exception as e:
        info["finished_at"] = time.time()
        info["error_text"] = str(e)
        info["status"] = "failed"
        task_log.update(info)
        err = f"❌ 后台任务 {task_id} 失败：{sanitize_error(e)}"
        logger.error(
            f"[OpenClaw bg] phase=end task_id={task_id} status=failed "
            f"error={type(e).__name__} total_s={time.time() - t0:.2f}",
            exc_info=True,
        )
        if has_recipient or (platform_meta and context):
            sent = await _try_send_result(
                real_event if has_recipient else None,
                err,
                task_id=task_id,
                platform_meta=platform_meta,
                context=context,
                kind="failed",
            )
            if not sent:
                logger.error(
                    f"[OpenClaw bg] phase=end task_id={task_id} status=failed_no_push "
                    f"reason=all_paths_failed（event.send 与 platform fallback 都失败，详见上面 push_via 日志）"
                )
    finally:
        async def _gc():
            await asyncio.sleep(3600)
            bg_tasks.pop(task_id, None)
            task_handles.pop(task_id, None)
        asyncio.create_task(_gc())
