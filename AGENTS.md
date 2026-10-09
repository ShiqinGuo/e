# 项目协作与工程约定

## Mandatory Python rules

- Use enums for states, discriminators, and other closed vocabularies. `Literal` may contain only members of an enum.
- Define cross-layer data contracts as Pydantic models in `domain`; use file-local dataclasses for internal records. Dictionaries must remain method-local and have an explicit `TypedDict` contract. Dynamic external protocol objects are validated and encapsulated at the protocol boundary, never used as business state.
- Do not use reflection, `getattr`, `setattr`, `hasattr`, `vars`, or `__dict__`. Do not probe dictionary keys with `.get`; a core orchestration dispatch registry is the only permitted exception when it improves clarity. HTTP, queue, and repository methods named `get` are unrelated.
- Put behavioral numeric limits in validated configuration or named defaults. Use enums for closed strings. Keep JSON decoding and encoding at request, response, persistence, or protocol boundaries; internal callers pass concrete objects. Remove compatibility branches and repeated defensive checks made unnecessary by boundary validation.
- Write source code and tests in English. Do not add comments or docstrings.
- Use `match/case` for parallel dispatch branches. Keep independent validation guards independent.
- Use Python 3.12 parameter syntax such as `[T]`; do not use `TypeVar`.
- Use absolute imports rooted at `agent_client`.
- Validate fields at construction and external boundaries, fail immediately on invalid input, and do not silently substitute defaults for invalid explicit configuration.

- 第一阶段实现通用 Python Agent client：异步执行内核、TUI、本地编码工具、个人 Skills、MCP、上下文、可观测性和恢复。矿业 server 与日报业务在后续阶段接入。
- 根目录仅保留 README.md 与 AGENTS.md；设计、操作指南及其它开发阶段 Markdown 文档统一在 docs/ 维护。当前设计唯一入口为 docs/DESIGN.md，不要另建重复设计。实现变化同步更新对应契约和验收方法。
- 设计中不确定或需要参考的机制，优先查看 OpenAI Codex 对应源码，记录固定 commit、源码路径、实际行为与本项目取舍，不凭产品印象推断实现。
- 教程仅提供思路，不直接照搬教学 mock、同步调用和全局状态。Agent 编排自行实现，允许协议 SDK、模型 SDK、TUI 库与数据校验等基础依赖。
- 遵循用户指定的 gongkao-hub Python 工程原则：可读性优先、协议与业务及持久化分离、依赖显式组装、边界使用 Pydantic 与具名类型、状态使用枚举、错误和配置集中管理。可信内部契约直接复用，避免透传包装、重复校验和宽泛异常兜底。
- 异步路径不直接执行阻塞 I/O；并发 task 不共享 AsyncSession；业务事务由 Service 组织，Repository 不隐藏提交，数据库事务中不等待模型、MCP 或命令执行。
- 使用 SQLAlchemy 类型化模型与表达式，不在业务中手写 SQL。数据库连接 PRAGMA 等配置集中在基础设施连接初始化处。迁移使用 Alembic，已发布迁移仅追加修正。
- 不维护无实际消费者的兼容层，不静默切换模型或吞错为空结果。外部 SDK 的标准协议协商须明确记录，不自行复制旧协议实现。
- 工具副作用结果未知时不自动重放。权限、取消、日志和数据库提交的边界依 docs/DESIGN.md 实现。
- 持久化同时使用 JSONL 与 SQLite：JSONL 记录会话事实，SQLite 为可重建状态投影。先等待日志持久化屏障，再在数据库事务内更新投影与游标；追平或重建不得触发工具重执行。诊断日志不是恢复事实来源。
- 第三方 API 写代码前先用 Context7 核对实际版本；索引过时或混版时以对应 tag 源码和官方文档为准。依赖使用 lockfile。
- 中文 Markdown 协作，代码采用清晰的英文标识。测试针对真实契约、异常、并发与恢复，不复述实现；持久化验证使用真实临时 JSONL 和 SQLite 文件，覆盖双写中断与投影重建。
- 临时脚本、日志、截图、调研源码与中间文件在仓库外本会话 outputs/；用户运行数据放独立用户数据目录。正式源代码、测试和设计文档按项目结构维护。
- 当前本地工作不自动扩展为远端创建、push、发布或业务功能实现；按用户请求推进。
