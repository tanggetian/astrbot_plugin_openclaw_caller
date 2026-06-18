"""astrbot_plugin_openclaw_caller core 子包

把 main.py 拆出的纯逻辑层（无 AstrBot Star 依赖）：
- util：常量、异常、类型转换、错误脱敏
- lite_event：LiteEvent mock（带 staticmethod 修复）
- access：白名单校验
- session：session_key 生成、system_prompt 渲染
- storage：SQLite 数据库 + TaskLog 类
- client：OpenClawClient（封装 /v1/chat/completions）
- runner：后台任务 runner
- api：Plugin Page Web API handlers

main.py 仅做薄入口：Star 注册、filter/llm_tool 装饰器、state 初始化。
"""
