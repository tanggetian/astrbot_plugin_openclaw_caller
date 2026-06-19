# 更新日志
## 1.3.1 - 2026-06-19

- **修复 插件市场拉取失败**：修复插件市场拉取失败

## 1.3.0 - 2026-06-16

- **修复 tool-sync 前台多轮会话锚点**：同步工具调用在未指定 `project`（或 `project=general`）时绑定到 AstrBot 当前会话（`unified_msg_origin + curr_conversation_id` 的 digest）；同一个 AstrBot 对话里的多次前台委派会稳定落到同一个 OpenClaw 对话。
- **支持前台指定项目对话**：同步工具调用只要显式传入 `project != general`，就会进入该项目桶，能续接之前同 project 的后台任务对话；例如后台 `project=research` 跑完后，前台追问也传 `project=research`。
- **强化 LLM 工具提示**：`delegate_to_openclaw` 明确提示主控 LLM 可用 `background=false` 前台同步模式和 OpenClaw 连续多轮对话，适用于追问、细化、澄清、补充、基于刚才结果继续处理。
- **保持后台行为不变**：`background=True`、`/oc bg` 仍沿用 project 分桶，不重发历史、不改 messages 构造，避免破坏已经正常工作的 OpenClaw `user` 字段 session 维护。
- **保留原 OpenClaw 多轮机制**：`core/client.py` 仍只发当前 user message，首轮注入 system prompt，后续依赖 OpenClaw Gateway 按 `user=session_key` 维护上下文；不再采用“每轮塞 SQLite 历史”的错误修复方案。
- **`/oc reset` 覆盖前台工具桶**：清空全部 session 时会额外清理当前 AstrBot 对话对应的 `tool-sync` session，避免前台工具对话残留。

## 1.2 - 2026-06-15

- **结构化 OpenClaw 调用日志**：每次同步调用生成 8 字符 `request_id`，自动关联 `phase=start / stream / end` 三条日志，`grep "request_id=xxx"` 拉一次完整生命周期。
- **后台任务 `task_id` 关联**：`/oc bg` 和 `delegate_to_openclaw(background=True)` 用 `task_id` 串起 `phase=start / end`；任务被取消 / 跑空 / 跑失败都有独立 status。
- **PII 脱敏日志字段**：`sender_id` / `session_key` 用 SHA1 前 8 字符 digest 展示——可关联但不可还原。
- **统一 `digest()` / `new_request_id()` 工具**：收口到 [core/util.py](core/util.py)，代码库再无散落的 `hashlib.sha1(...)` / `uuid.uuid4().hex[:8]` 临时拼装。
- **核心模块拆分**：原 1327 行的 `main.py` 拆成 9 个 `core/` 子模块（约 1027 行），`main.py` 瘦身为 607 行。
- **状态全部封装**：模块级 `_module_cfg` 字典 + 全局变量全部迁到 `OpenClawCaller` 实例字段，单测可挂任意配置跑。
- **错误码可分类**：所有失败路径都打 `status=ok / timeout / connection_error / http_error / empty_response / done / cancelled / failed / ...` 中的一种，统计 / 告警友好。
- **连接中断时返回部分响应**：已收到内容 + 连接中断 / 超时时返回 `chunks` 拼出的部分响应并附 `⚠️` 提示；新增 `status=partial_response`。
- **后台任务完成时双路径推送**：`event.send()` 为主路径，`context.get_platform().send_message()` 为 fallback（绕开 event 生命周期）；新增 `platform_meta` / `context` 参数；新增日志 `push_via=event_send | event_send_failed | platform_fallback | platform_fallback_failed`。
- **配置默认值**：`openclaw_timeout` **300s → 1800s**（30 分钟，长 agent 任务更友好；走反向代理时记得同步调大 `proxy_read_timeout`）。
- **新增 [DEBUG.md](DEBUG.md)**：技术性字段定义、status 列表、grep 调试示例迁出 README；README 仅留一行指针。
- 修复 `client.py` SSE 解析器嵌套 `if` 缩进对齐（首字延迟判断在某些路径下会被嵌套遮蔽，统计时长偏小）。
- 补齐 `core/util.py` 之前缺失的 `digest` / `new_request_id` 工具（之前各模块临时拼装 SHA1 / UUID，重复且风格不一）。
- **不破坏兼容**：所有命令、工具、配置项、Plugin Page 行为和 v1.1 100% 一致——直接覆盖安装即可。

## 1.1

- 基础功能：手动 `/oc` 命令、LLM 工具 `delegate_to_openclaw` / `get_openclaw_task_result`、后台任务、Plugin Page 任务列表。
- 多轮对话：首轮 system prompt 注入，本地 SQLite 留最近 1 轮审计。
- 安全：白名单、Token 隔离、`sanitize_error()` 用户侧脱敏。
- 核心：`OpenClawClient` 封装 `/v1/chat/completions` 流式调用 + SSE 解析。
