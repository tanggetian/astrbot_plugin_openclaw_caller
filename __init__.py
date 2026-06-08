"""astrbot_plugin_openclaw_caller

AstrBot ↔ OpenClaw Gateway 桥接插件。

把 AstrBot 主控 LLM 收到的长任务，通过 OpenClaw Gateway 的
OpenAI 兼容 /v1/chat/completions 端点委派给任意 Agent 执行。

核心能力：
- /oc [project] <任务>：手动命令触发（project 完全动态）
- /oc bg [project] <任务>：手动后台任务，跑完主动通知
- /oc reset [project]：清空历史（不填清全部）
- delegate_to_openclaw：Function Calling 工具，让主控 LLM 自主决策
- get_openclaw_task_result：Function Calling 工具，让主控 LLM 读取 OpenClaw 返回结果
- 多轮对话：OpenClaw Gateway 按 user 字段自动维护 session
- 后台任务：长任务可设 background=True，主对话不阻塞
- SQLite 审计：插件独立数据库记录任务生命周期；不重发 OpenClaw 历史
- 权限控制：默认白名单——access_control.allowed_user_ids 列表内用户可调用
- 项目隔离：project 字符串动态生成独立 session

适用场景：把主控 LLM 不想干、不会干、干得慢或干不了的任务
外包给本地或远程的专家 Agent 执行（如网络安全扫描、代码生成、
长文本处理、文件分析、数据库操作、自动化运维等）。

配置说明：所有运行时配置（OpenClaw URL、Token、Agent ID、超时、
系统提示词）均在 AstrBot WebUI 配置页填写；白名单与 SSL 验证默认开启，
OpenClaw URL/Token 需手动填写。

作者：唐格天（花翎协助调测）
版本：1.1.0
许可：MIT
"""

__version__ = "1.1.0"
