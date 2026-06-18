# OpenClaw Caller — 调试与日志

> **本文件面向管理员 / 高级用户**。普通用户请看 [README.md](README.md)。
>
> 这里的内容**不进 README**——README 面向最终用户，只描述「怎么用」；本文档面向「装好之后遇到问题怎么查」。

---

## 1. 日志位置与级别

所有调用走 AstrBot 自带 logger，tag 是 `[Plug]`，落点取决于 AstrBot 自身的日志配置（通常是 `data/logs/astrbot.log`）。

**1.2 起全部日志带关联 key**：

- `request_id`（8 字符 UUID）—— 关联一次同步调用的 `phase=start / stream / end` 三条
- `task_id`（`bg-` 开头）—— 关联一次后台任务的 `phase=start / end` 两条

管理员 `grep "request_id=abc12345"` 就能拉出一次 OpenClaw 调用的完整生命周期。

---

## 2. 同步调用日志样例

`/oc` 命令或 `delegate_to_openclaw` 同步调用产 3 行（`start` / `stream` / `end`）：

```text
[OpenClaw cmd] cmd=/oc project=research sender=a1b2c3d4 session=k9l8m7n6 task_chars=120
[OpenClaw call] phase=start request_id=abc12345 project=research sender=a1b2c3d4 session=k9l8m7n6 task_chars=120 has_sp=true sp_injected=true
[OpenClaw call] phase=stream request_id=abc12345 first_chunk_s=1.23
[OpenClaw call] phase=end request_id=abc12345 status=ok chunks=15 first_chunk_s=1.23 total_s=8.45 response_chars=2048
```

---

## 3. 后台调用日志样例

`/oc bg` 命令或 `delegate_to_openclaw(background=True)` 用 `task_id` 关联：

```text
[OpenClaw cmd] cmd=/oc_bg project=research sender=a1b2c3d4 session=k9l8m7n6 task_chars=200
[OpenClaw bg] phase=start task_id=bg-1717854321000-ab12cd sender=a1b2c3d4 session=k9l8m7n6 task_chars=200 has_platform_fallback=true
[OpenClaw call] phase=start request_id=ef34gh56 ...   # 内部还会发一次同步调用，由 core/client.py 出
[OpenClaw call] phase=end request_id=ef34gh56 status=ok ...
[OpenClaw bg] phase=end task_id=bg-1717854321000-ab12cd status=done total_s=10.12 push_via=event_send
```

---

## 4. 推送路径（后台任务）

后台任务完成时按顺序尝试两条路径，确保结果送达用户：

| 路径 | 角色 | 触发 | 日志 `push_via=` |
|---|---|---|---|
| 1️⃣ `event.send()` | **主路径** | 真 AstrMessageEvent 仍在生命周期内 | `event_send` / `event_send_failed` |
| 2️⃣ `context.get_platform().send_message()` | **fallback** | 路径 1 失败 / LiteEvent / event 已 finalize | `platform_fallback` / `platform_fallback_failed` |

只有当两条都失败时才标 `no_recipient`（之前 v1.1 只要 `event.send` 失败就标）。

`has_platform_fallback` 字段在 `phase=start` 日志里：true = 已成功抓出 `(platform_name, session_id)`；false = 没法 fallback（极少见的真无推送目标场景）。

```text
# 正常推送（主路径成功）
[OpenClaw bg] phase=end task_id=bg-... status=done push_via=event_send

# 主路径失败，fallback 成功
[OpenClaw bg] phase=end task_id=bg-... status=done push_via=event_send_failed error=...（尝试 platform fallback）
[OpenClaw bg] phase=end task_id=bg-... status=done push_via=platform_fallback platform=aiocqhttp

# 两条都失败
[OpenClaw bg] phase=end task_id=bg-... status=done push_via=all_failed（event.send + platform fallback 都失败）
```

后台任务若没有真 AstrMessageEvent 注入（LiteEvent fallback），会额外一条 warn：

```text
[OpenClaw bg] phase=start task_id=bg-... event_is_lite=true（框架没把真 AstrMessageEvent 注入到 Tool args）。结果仅写 SQLite，Plugin Page 标 no_recipient。
```

---

## 5. 失败 / 错误 status 列表

| status | 出现位置 | 含义 |
|---|---|---|
| `ok` | sync `phase=end` | 调用成功 |
| `partial_response` | sync `phase=end` | **优雅降级**——OpenClaw 推了一段后连接被掐（Gateway OOM / 反向代理 idle kill），插件**不**整段失败，而是把已收到的内容拼上 `⚠️` 提示返回 |
| `timeout` | sync `phase=end` | aiohttp ClientTimeout（默认 1800s）触发 |
| `connection_error` | sync `phase=end` | aiohttp ClientError（DNS 失败 / 连接被拒 / TLS 失败等） |
| `http_error` | sync `phase=end` | OpenClaw 返回 4xx / 5xx（带具体 status code） |
| `empty_response` | sync `phase=end` | SSE 流跑完但 chunks 为空 |
| `unknown_error` | sync `phase=end` | 其他未分类异常 |
| `done` | bg `phase=end` | 后台任务成功，**且推送成功** |
| `no_recipient` | bg `phase=end` | 任务成功但**所有推送路径都失败**（结果已存 SQLite，可在 Plugin Page 查） |
| `done_no_recipient` | bg `phase=end` | 同上，旧字段名（v1.1 兼容） |
| `failed` | bg `phase=end` | 任务执行抛异常 |
| `failed_no_push` | bg `phase=end` | 任务异常 + 推送也失败 |
| `cancelled` | bg `phase=end` | asyncio 被取消（Plugin Page 删任务时主动取消） |
| `done_no_recipient` | bg `phase=end` | LiteEvent + 无 platform fallback（任务完成但没法推） |
| `running` | bg `phase=start` | 后台任务已调度，还没跑完 |

`exc_info=True` 时完整 traceback 进 AstrBot 日志——管理员从日志看根因，用户只看到 `ExceptionType（详情见 AstrBot 日志）`。

---

## 6. 字段说明

| 字段 | 含义 |
|---|---|
| `request_id` | 8 字符 UUID，**一次** OpenClaw 调用的关联 key |
| `task_id` | 后台任务的稳定 ID（`bg-` 或 `tool-` 开头） |
| `project` | 项目名 / 会话桶——LLM 传的或者 `/oc <project> <任务>` 解析出的 |
| `sender` | sender_id 的 SHA1 前 8 字符 digest——可关联但不可还原 |
| `session` | session_key 的 SHA1 前 8 字符 digest |
| `task_chars` | 任务文本的字符数（**不进**原文本） |
| `has_sp` / `sp_injected` | 是否配置 / 实际注入了 system prompt 模板 |
| `first_chunk_s` | 流式首字延迟（秒） |
| `total_s` | 整次调用总时长（秒） |
| `response_chars` | 响应文本的字符数（**不进**原文本） |
| `chunks` | 收到 SSE chunk 数量 |
| `push_via` | 后台任务推送路径（`event_send` / `event_send_failed` / `platform_fallback` / `platform_fallback_failed` / `all_failed`）——仅后台任务 `phase=end` 日志带 |
| `has_platform_fallback` | 后台任务 `phase=start` 日志带：`true` = 已抓出 `(platform_name, session_id)` 可走平台 fallback；`false` = 走不到 fallback |
| `event_is_lite` | 后台任务 `phase=start` 日志带：true = 框架没把真 event 注入，结果仅入库 |

`session_key` / `user_id` / `task` 文本 / `result` 文本**不写入日志**（仅 session_digest 哈希、task_chars 字符数），避免泄露用户隐私。

---

## 7. 调试示例

### 7.1 找一次失败的调用

```bash
# 1. 先在 AstrBot 日志里 grep 失败的调用
grep "phase=end status=failed" /path/to/astrbot.log

# 2. 拿到 request_id=ab12cd34 后
grep "request_id=ab12cd34" /path/to/astrbot.log
```

### 7.2 找一次超时

```bash
grep "status=timeout" /path/to/astrbot.log
```

### 7.3 看错误分布

```bash
grep -oE "status=[a-z_]+" /path/to/astrbot.log | sort | uniq -c | sort -rn
```

### 7.4 列出所有后台任务

```bash
grep "OpenClaw bg" /path/to/astrbot.log | grep "task_id=bg-"
```

### 7.5 看推送路径分布

```bash
grep -oE "push_via=[a-z_]+" /path/to/astrbot.log | sort | uniq -c | sort -rn
```

### 7.6 找没推送到的任务（结果只能从 Plugin Page 查）

```bash
grep "status=no_recipient" /path/to/astrbot.log
```

---

## 8. 日志脱敏

为避免在日志里泄露用户隐私：

- **不进日志**：完整 `openclaw_url` / `openclaw_token` / `sender_id` / `session_key` 原值 / 任务文本 / 响应文本
- **进日志**：sender / session 的 SHA1 前 8 字符 digest（可关联同用户多次调用但不可还原）；任务 / 响应的字符数

如果发现日志里出现了原值，**请立即提 issue**——这是隐私 bug。
