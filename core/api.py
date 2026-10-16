"""Plugin Page Web API handlers——从 OpenClawCaller 类中拆出。

所有 handler 都是纯函数，参数注入：``task_log``、``bg_tasks``、``task_handles``。
行为与原 ``_api_list_tasks`` / ``_api_cancel_task`` / ``_api_delete_task`` **完全一致**。

【v1.4.1 兼容修复】
- 显式从 ``astrbot.api.web`` 拿 ``json_response`` / ``request``，
  不再依赖 Quart 的兼容垫片。AstrBot 4.x 的 dashboard 是 FastAPI，
  Quart 适配层仍能跑但官方文档明确建议用 ``astrbot.api.web``。
- 响应体形态保持 ``{"ok": bool, ...}`` 不变——前端 ``app.js`` 不需要改。
- ``request.get_json() or {}`` → ``request.json(default={})``：
  两边行为等价（None / 解析失败时落到 ``{}``），但后者是当前 API。
"""
from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import json_response, request


async def api_list_tasks(task_log) -> Any:
    """GET /api/plug/astrbot_plugin_openclaw_caller/tasks

    返回字段已经过脱敏：不返 user_id / result_text / error_text。
    """
    from .util import sanitize_error
    try:
        tasks = task_log.list_recent(limit=200)
        return json_response({
            "ok": True,
            "tasks": tasks,
            "running_count": sum(1 for t in tasks if t["status"] == "running"),
            "total_count": len(tasks),
        })
    except Exception as e:
        logger.error(f"_api_list_tasks 异常：{e}", exc_info=True)
        return json_response(
            {"ok": False, "error": sanitize_error(e), "tasks": []},
            status_code=500,
        )


async def api_cancel_task(task_log, bg_tasks, task_handles) -> Any:
    """POST /api/plug/astrbot_plugin_openclaw_caller/cancel body: {"task_id": "bg-..."}"""
    from .util import sanitize_error
    try:
        data = await request.json(default={})
        task_id = (data.get("task_id") or "").strip()
        if not task_id:
            return json_response(
                {"ok": False, "error": "task_id required"},
                status_code=400,
            )
        info = bg_tasks.get(task_id)
        if not info:
            return json_response(
                {"ok": False, "error": "task not found or already finished"},
                status_code=404,
            )
        handle = task_handles.get(task_id)
        info["status"] = "cancelled"
        info["finished_at"] = time.time()
        task_log.update(info)
        if handle and not handle.done():
            handle.cancel()
        return json_response({"ok": True, "task_id": task_id, "status": "cancelled"})
    except Exception as e:
        logger.error(f"_api_cancel_task 异常：{e}", exc_info=True)
        return json_response(
            {"ok": False, "error": sanitize_error(e)},
            status_code=500,
        )


async def api_delete_task(task_log, bg_tasks, task_handles) -> Any:
    """POST /api/plug/astrbot_plugin_openclaw_caller/delete body: {"task_id": "..."}"""
    from .util import sanitize_error
    try:
        data = await request.json(default={})
        task_id = (data.get("task_id") or "").strip()
        if not task_id:
            return json_response(
                {"ok": False, "error": "task_id required"},
                status_code=400,
            )
        info = bg_tasks.pop(task_id, None)
        handle = task_handles.pop(task_id, None)
        if handle and not handle.done():
            handle.cancel()
        deleted = task_log.delete(task_id)
        if not deleted and not info:
            return json_response(
                {"ok": False, "error": "task not found"},
                status_code=404,
            )
        return json_response({"ok": True, "task_id": task_id, "deleted": True})
    except Exception as e:
        logger.error(f"_api_delete_task 异常：{e}", exc_info=True)
        return json_response(
            {"ok": False, "error": sanitize_error(e)},
            status_code=500,
        )
