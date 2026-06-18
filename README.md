# astrbot_plugin_openclaw_caller

AstrBot ↔ OpenClaw Gateway 桥接插件。

仓库：https://github.com/tanggetian/astrbot_plugin_openclaw_caller

把 AstrBot 主 LLM 收到的长任务，通过 OpenClaw Gateway 的 OpenAI 兼容 `/v1/chat/completions` 端点委派给 OpenClaw Agent 执行。

## ✨ 核心能力

- 手动命令：`/oc [project] <任务>`、`/oc bg [project] <任务>`、`/oc reset [project]`
- Function Calling：`delegate_to_openclaw` Tool 负责下发任务，`get_openclaw_task_result` Tool 负责读取结果供主 LLM 分析
- 会话分桶：按平台、用户、project 和 system prompt 版本生成独立 session
- 后台异步：长任务不阻塞主对话
- 任务列表 Page：在 AstrBot WebUI 插件详情页查看运行中 / 历史任务
- 项目分桶：project 字符串**完全动态**，任意合法字符串都形成独立 session

## 📦 安装

1.将本插件目录放到 AstrBot 的 `data/plugins/` 下，重启 AstrBot。

2.在astrbot插件市场搜索openclaw任务委派安装

## ⚙️ 配置

在 AstrBot WebUI 插件配置页填写：

| 配置项 | 说明 |
|--------|------|
| `openclaw_url` | OpenClaw Gateway 根 URL（不含端点路径，插件自动拼 `/v1/chat/completions`） |
| `openclaw_token` | OpenClaw Bearer Token（必填） |
| `openclaw_agent_id` | OpenClaw agent ID（默认 `main`） |
| `openclaw_timeout` | 请求总超时（秒，默认 1800 = 30 分钟） |
| `openclaw_verify_ssl` | 是否验证 OpenClaw HTTPS 证书（默认开启；仅自签名/本地场景建议关闭） |
| `openclaw_system_prompt` | 每个 project/session_key 首次任务开始时发给 OpenClaw 的 system message 模板（留空不发送；支持 `{project}`、`{user_id}`、`{session_key}`） |
| `access_control` | 用户白名单（**默认开启**——首次使用需在 WebUI 填 `allowed_user_ids` 列表） |

> **安全默认值**：白名单默认开启，SSL 证书验证默认开启；`openclaw_url` 和 `openclaw_token` 需手动填写。

## 🔐 权限（默认白名单）

本插件**默认启用白名单**——只有 `access_control.allowed_user_ids` 列表中的用户能调用。

**首次使用步骤**：

1. 打开 AstrBot WebUI → 插件配置页
2. 找到 `access_control` → 展开 `items`
3. 确认 `whitelist_enabled` 已勾选（默认开）
4. 在 `allowed_user_ids` 填入允许的用户 ID（JSON 数组，如 `["123456"]`）
5. 保存配置并重启 AstrBot

**获取用户 ID**：私聊 AstrBot 发任意消息，然后看 AstrBot 日志中的 `sender_id`。

**关闭白名单**：取消勾选 `whitelist_enabled`（不推荐，所有用户都能调用）。

## 🚀 使用方法

### 1. 手动命令

```text
/oc <任务>                  # 默认 general
/oc project-a <任务>        # 指定 project
/oc bg project-a <长任务>   # 后台跑，跑完主动通知
/oc reset                   # 清空所有项目历史
/oc reset project-a         # 只清空指定项目
```

### 2. 让 AstrBot 主 LLM 自主调用

直接描述需求即可，例如让 OpenClaw agent 跑一个扫描任务。AstrBot 主 LLM 会自主判断是否需要委派、用哪个 project、是否后台。

插件会把 `delegate_to_openclaw` 注册为 AstrBot Function Calling 工具。主控 LLM 遇到以下场景应主动调用：

- 扫描、调研、代码生成、文件/数据分析、联网查询、自动化运维、外部系统操作
- 用户明确说“交给 agent / OpenClaw / 后台跑 / 长任务 / 帮我执行 / 跑一下”
- 任务预计超过 30 秒，或主控 LLM 无法直接在当前对话里可靠完成
- 用户要继续追问、细化、澄清、补充、让 OpenClaw 基于刚才结果继续处理时，可使用前台同步模式（`background=false`）和 OpenClaw 进行连续多轮对话

如果模型仍不主动调用，请检查 AstrBot 当前 Provider 是否启用了 Function Calling / Tool Call 能力，并确认插件详情页工具列表里存在 `delegate_to_openclaw`。

主控 LLM 应按用户语义选择不同 `project`：调研类可用 `research`，代码类可用 `code`，扫描类可用 `scan`，运维类可用 `ops`。同一 `project` 会共享 OpenClaw 端上下文，不相关任务不要复用同一桶。后台任务完成后，如果用户要在前台继续和该项目 agent 沟通，必须继续传同一个 `project`；不传或传 `general` 时，前台同步会绑定当前 AstrBot 对话。

后台任务完成后，结果会推送给用户并写入插件独立 SQLite 数据库。用户要求“分析/总结/解释刚才 OpenClaw 返回结果”时，主控 LLM 应调用 `get_openclaw_task_result`，按 `task_id` 或最近任务读取结果后再分析，避免看不到异步推送内容而编造。

### 3. 后台推送失效时的降级

```text
我：AstrBot，OpenClaw agent 那边跑到哪了？
AstrBot：好的主人，我去问 agent……
```

## 📋 查看任务列表

本插件自带 Dashboard Page，展示运行中和历史任务摘要。查看步骤：

1. 打开 AstrBot WebUI
2. 进入 **插件** 页
3. 找到 `astrbot_plugin_openclaw_caller` 卡片，点击进入插件详情
4. 在详情页点击 **任务列表** 入口

页面上可以看到：

- **运行中表格**：当前正在跑的 background 任务（项目 / 任务 / 模式 / 创建时间 / task_id）
- **历史表格**：已完成、失败、取消、无推送的任务（项目 / 任务 / 状态 / 模式 / 创建 / 完成 / 耗时 / task_id）
- **统计计数**：运行中 / 已完成 / 失败 / 无推送 / 总计
- **任务管理**：每行提供删除按钮；运行中任务会先取消后台协程再删除记录
- **自动刷新**：每 5 秒拉取最新数据

`no_recipient` / 「无推送」表示任务已结束并写入插件独立 SQLite 数据库，但当时没有可用的真实消息事件用于主动推送。Plugin Page 面向 AstrBot Dashboard 管理侧使用，任务列表默认只展示任务摘要，不返回 OpenClaw 结果正文。

## 🧪 system_prompt 示例

`openclaw_system_prompt` 只会在每个 project/session_key 第一次任务开始时注入给 OpenClaw agent；同一桶后续任务不重复发送，也不会加到 AstrBot 主控 LLM。首轮采用双保险：既作为 OpenAI `messages[0].role=system` 发送，也会内嵌到第一条 user 任务开头，避免 OpenAI 兼容 Gateway 不持久化 system role 导致提示词丢失。可按 project 配置模板变量：

模板内容会参与 `session_key` 版本号计算：首次配置或修改模板后，插件会自动开启新的 OpenClaw 会话桶，确保提示词位于项目最开头；旧桶不会被继续复用。

```text
我是xxx（AstrBot 机器人的名称），是主人的调度助手，目前转达主人命令。
```


## 🐛 故障排除

遇到问题请按以下顺序检查：

1. **检查链接**：确认 `openclaw_url` 拼写正确，端口可达，OpenClaw 服务在运行
2. **检查 OpenClaw**：用 `curl <openclaw_url>/v1/models` 测试 Gateway 是否在线
3. **检查 token**：确认 `openclaw_token` 与 OpenClaw 控制台一致，未过期
4. **检查日志**：在 AstrBot 日志中搜索 `[openclaw_caller]` 关键字
5. **检查配置**：确认 WebUI 中 `openclaw_url`、`openclaw_token`、`openclaw_agent_id`、`openclaw_timeout`、`openclaw_verify_ssl` 等配置正确，URL 末尾不要带 `/v1/chat/completions`

> 本插件启动时会在日志中输出 `[openclaw_caller] 初始化完成: url_configured=..., agent_id=..., has_openclaw_system_prompt=...`，可用于确认配置读取是否正确；不会输出 token。

## 📑 调试 / 日志

> **详细字段定义、grep 示例、status 列表、推送路径升级**等管理侧内容，统一在 [DEBUG.md](DEBUG.md) 里维护。本 README 只面向「怎么用」的用户视角——遇到问题时知道去 DEBUG.md 查。

## 🔐 安全说明

- **白名单**：`access_control.whitelist_enabled=True` 时只有 `allowed_user_ids` 列表里的用户能调；`block_when_disabled=True` 时未授权调用静默拒绝。
- **LLM Tool 安全门**：`delegate_to_openclaw` 不暴露 `sender_id` 参数——AstrBot 注入的真 event 是 sender_id 的唯一来源，LLM 无法伪造。
- **数据库**：每个 AstrBot 实例的 openclaw_sessions / openclaw_tasks 表彼此隔离（独立 SQLite 在 `data/plugins/astrbot_plugin_openclaw_caller/openclaw_caller.db`）。
- **session_key 隔离**：`<platform>-<sender_id>-<mode>-<project>-<sp_version>`，不同平台/不同用户/不同项目/不同 system prompt 版本各自独立 OpenClaw session。
- **日志脱敏**：完整 URL / Token / sender_id / task 文本 / 响应文本**不进** AstrBot 日志；只记 digest（SHA1 前 8 字符）+ 字符数。
- **首轮 system prompt 双保险**：每次开新 session 时，既发 `messages[0].role=system`，也把模板内嵌到第一条 user 消息开头——兼容不持久化 system role 的 OpenAI 兼容 Gateway。

## 🛠 配置项详解

| key | 类型 | 必填 | 说明 |
|---|---|---|---|
| `openclaw_url` | str | ✅ | OpenClaw Gateway 根 URL（**不带** `/v1/chat/completions`） |
| `openclaw_token` | str | ✅ | Bearer Token |
| `openclaw_agent_id` | str | ❌ | 默认 `main` |
| `openclaw_timeout` | int | ❌ | 默认 `1800` 秒，前台/后台 OpenClaw 调用共用同一超时 |
| `openclaw_verify_ssl` | bool | ❌ | 默认 `True`，自签名证书场景关掉 |
| `openclaw_system_prompt` | str | ❌ | 发给 OpenClaw 的 system message 模板；支持 `{project}` / `{user_id}` / `{session_key}` 占位符。示例：我是xxx（AstrBot 机器人的名称），是主人的调度助手，目前转达主人命令。 |
| `access_control.whitelist_enabled` | bool | ❌ | 默认 `True`——不开启时所有用户可调 |
| `access_control.allowed_user_ids` | list[str] | ❌ | 白名单用户 ID 列表（按 AstrBot 平台规范，aiocqhttp 用 QQ 号字符串） |
| `access_control.block_when_disabled` | bool | ❌ | 默认 `False`——未授权时是否给用户明确提示 |

## 📂 文件结构

```text
astrbot_plugin_openclaw_caller/
├── __init__.py                # 插件元数据 & 默认配置
├── _conf_schema.json          # WebUI 配置 schema
├── main.py                    # Star 入口（薄——纯装饰器 + state 初始化）
├── metadata.yaml              # 插件市场元数据
├── README.md
├── requirements.txt           # aiohttp>=3.11
├── core/                      # ★ 1.2.0 新增子包——把 main.py 业务逻辑全拆出
│   ├── __init__.py
│   ├── util.py                # PLUGIN_NAME / OpenClawError / sanitize_error / to_bool / digest / new_request_id
│   ├── lite_event.py          # LiteEvent mock（带 staticmethod 修复）
│   ├── access.py              # 白名单 check_allowed
│   ├── session.py             # session_key 生成 / system_prompt 渲染 / 项目名归一化 / prompt 解析
│   ├── storage.py             # SQLite + TaskLog 类（openclaw_sessions + openclaw_tasks CRUD）
│   ├── client.py              # OpenClawClient（封装备 /v1/chat/completions + 结构化日志）
│   ├── runner.py              # background_run（参数注入，后台任务生命周期 + 结构化日志）
│   └── api.py                 # Plugin Page Web API handlers（list / cancel / delete）
└── pages/openclaw-tasks/      # 插件页面
    ├── index.html
    ├── style.css
    └── app.js
```
