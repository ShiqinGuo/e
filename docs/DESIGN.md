# 通用异步 Agent Client 设计

本文定义一个可独立使用的 Python coding-agent client。用户通过 TUI 与模型协作，Agent 可以读取和修改代码、执行命令、使用个人 Skills 与 MCP 工具，并在长任务、断网、中断和进程退出后保留可解释、可恢复的状态。第一阶段不实现矿业 server；后续矿业日报作为同一个 client 的任务配置和 MCP 接入场景。

本文是当前设计的唯一入口。设计、操作指南及其它开发阶段 Markdown 文档统一在 docs/ 维护，根目录保留 README.md 与 AGENTS.md。已支持的操作与启动步骤见 [README.md](../README.md)，本地操作步骤见 [USER_GUIDE.md](USER_GUIDE.md)。

## 1 目标与设计约束

第一阶段交付一套可日常试用的单 Agent client，包含：

- TUI 与非交互 CLI，共用异步执行内核。
- 流式模型交互、多轮工具调用、取消、运行预算和用户消息队列。
- 工作区文件检索、读取、补丁修改与命令执行。
- 可配置个人指令、Skills 根目录、模型连接和 MCP server。
- 独立的 OpenAI 订阅登录与令牌生命周期，参考 gongkao-hub 的官方 Sign in with ChatGPT 实现。
- 稳定提示前缀、缓存观测、工具与技能按需加载。
- 可控的上下文压缩、完整历史与大输出取回。
- JSONL 会话事实记录、SQLite 状态索引、工具执行记录、压缩检查点和故障恢复。
- 本地结构化日志、关键运行指标及 OpenTelemetry Run span；远端导出器作为后续接入点。

Agent 编排手写，不使用 LangChain、LangGraph 或现成 Agent runtime。允许使用模型/MCP SDK、Textual、Pydantic、SQLAlchemy 等基础库。Python 使用 3.12+，以 Windows 为首个实际验收环境，同时设计 POSIX 命令适配。

首版范围不含多 Agent、浏览器自动化、任务调度平台、向量数据库、插件市场和后台常驻服务。支持个人 Skill 文件不等于实现插件安装平台。支持本地 shell 不等于实现 OS 沙箱；实际权限必须明确呈现。

设计不确定时优先看 Codex 源码；借鉴机制与边界，不复制其产品规模、Rust 目录结构或内部服务依赖。

## 2 参考基线与已核实事实

### 2.1 固定源码版本

设计参考固定源码版本：

- OpenAI Codex：commit **19b7bffd7bd5c325a45b91111ce64c85610b90ba**。
- MCP Python SDK：tag **v2.3.0**，commit **2118f14f8a19bc158d8a1cf90af58d85d187f849**。
- 教程：用户本地 learn-claude-code 阅读版，上游固定 commit ce8f9f186058939da54c9d6fead78dfb5d0fd6c3。主要参考第 1、2、8、14、17 课。
- 工程规范：用户指定的 D:/code/gongkao-hub/src/AGENTS.md 及其继承的根 AGENTS.md。提取通用 Python 原则，不引入公考业务、部署和旧包迁移规则。

下面的 Codex 行为来自源码检查；“本项目决定”是我们的设计，不代表 Codex 原样采用。

| 关注点 | Codex 源码中的实际机制 | 本项目决定 |
| --- | --- | --- |
| 前缀缓存 | client.rs 生成稳定 prompt_cache_key；请求保留模型原生响应信息 [C1] | 稳定前缀与序列化、显式上下文版本、记录缓存用量 |
| 压缩触发 | context_window.rs 区分完整上下文硬上限、自动压缩预算与轮次结束阈值 [C2] | 使用 token 预算和安全余量，不按消息条数决定 |
| 压缩与恢复 | compact.rs 替换活动历史；rollout_reconstruction.rs 从检查点及后续记录重建 [C3][C4] | 原始历史不删，完整检查点作为一条 JSONL 记录提交，再更新 SQLite 投影 |
| 调用配对 | normalize.rs 为缺失结果补明确的中止结果，并稳定合成 ID [C5] | 恢复时按真实执行状态生成失败或未知结果，不伪造成功 |
| 持久化 | rollout 有 JSONL 写入队列和 flush 屏障；state 另有 SQLite 状态、队列和日志存储 [C6][C7] | 同时采用 JSONL 与 SQLite；前者记录会话事实，后者维护可重建的查询与状态投影 |
| 写者所有权 | rollout/writer_lock.rs 使用跨进程文件锁确保单线程记录写者 [C8] | 会话执行及 JSONL 追加使用 OS 文件锁，SQLite 投影使用数据库事务 |
| Skills | 有独立的目录发现、元数据解析、渲染预算和顺序控制 [C9] | 稳定目录摘要、按需正文、来源与内容版本固定 |
| 项目指令 | agents_md.rs 按项目根到工作目录发现规则；manager 管理验证后的快照 [C10] | 根规则与目标目录规则分别加载并记录作用域 |
| 工具规模 | mcp_tool_exposure.rs 支持直接或延迟暴露；tool_search.rs 独立管理工具检索 [C11] | MCP 工具先检索后调用，避免所有 schema 挤进前缀 |
| 代码检索 | 提示模板建议 rg [C15]；file-search 另用 ignore walker 与模糊匹配处理文件选择 [C12] | 区分正文检索与路径补全，不把两者说成同一实现 |
| 可观测性 | otel 有模型流、工具决策、耗时及缓存 token 等事件 [C13] | 区分恢复事件、模型用量与可丢弃的诊断日志 |
| TUI | AppEvent 作为 UI 组件与应用循环之间的消息通道 [C14] | TUI 发送命令、订阅事件，不拥有 Agent 业务状态 |

教程第 14 课明确使用进程内 mock MCP，不含真实 transport。仅保留其发现、命名与分发思想，实际协议由官方 SDK 处理。

### 2.2 依赖基线

| 部分 | 选择 | 边界 |
| --- | --- | --- |
| 异步执行 | asyncio | 一个应用事件循环，显式管理任务生命周期 |
| 类型与配置 | Pydantic 2、TOML | 外部边界校验，内部复用类型 |
| TUI | Textual | 只处理显示、输入与界面生命周期 [D3] |
| 首个模型适配 | 官方 Responses 协议、httpx 异步流 | ChatGPT 订阅 OAuth 与显式 API key 两种认证互斥配置，不引入参考项目的 LangChain 适配层 |
| MCP | 官方 Python SDK v2.3.0 | stdio、Streamable HTTP；锁定依赖后做真实连接验证 [M1] |
| 持久化 | JSONL writer、SQLAlchemy 2 asyncio、aiosqlite、SQLite | 日志先行、数据库投影；单机本地磁盘；aiosqlite 在线程中执行 SQLite [D4] |
| 迁移 | Alembic | 首个 schema 也通过迁移创建 |
| 检索 | ripgrep | 启动检查版本与路径；作为显式运行依赖 |
| 可观测性 | Python logging、OpenTelemetry | 本地日志默认开启，远端导出默认关闭 |

首个模型适配使用 Responses，并按用户要求接入 OpenAI 订阅登录；默认模型配置使用 gpt-6.1-sol，可由用户显式修改。当前适配器显式区分订阅与 API key 的请求参数；不支持的原生压缩明确拒绝，模型上下文窗口由配置给出。其他供应商通过新适配器增加，不以改 base_url 就宣称协议兼容；不静默换模型。实际依赖版本锁定在 uv.lock。

MCP 协议以 v2.3.0 tag 源码与 protocol-versions.md 为准；参考示例需要区分所属协议版本。

## 3 总体架构与职责

~~~mermaid
flowchart TD
    TUI[TUI 与 CLI] -->|命令| Session[SessionService]
    Session --> Runtime[AgentRuntime]
    Runtime --> Context[ContextManager 与 PromptBuilder]
    Runtime --> Model[ModelGateway]
    Runtime --> Executor[ToolExecutor]
    Executor --> Local[本地工具]
    Executor --> MCP[McpManager]
    Runtime --> Store[持久化协调器]
    Context --> Store
    Session --> Skills[SkillCatalog 与 InstructionResolver]
    Store --> Journal[JSONL 会话事实]
    Journal --> Projector[幂等状态投影]
    Projector --> DB[(SQLite)]
    Store --> Blobs[不可变大输出与文件快照]
    Runtime -->|事件| TUI
    Runtime --> Telemetry[日志与 Trace]
~~~

应用在同一进程运行，首版不额外创建 HTTP 后端。保留清楚的 Python 接口，使 headless CLI、TUI 和测试均使用同一个 Runtime。

- SessionService：创建、加载会话，接收用户命令，维护输入队列与单写者所有权。
- AgentRuntime：一条模型决策循环，控制模型请求、工具批次、停止与取消。
- ContextManager：构造活动上下文、计量、压缩与检查点，保留完整历史引用。
- PromptBuilder：确定性的前缀与消息渲染，管理 prompt revision。
- ToolRegistry / ToolExecutor：定义、路由、参数校验、权限、并发与结果提交。
- SkillCatalog / InstructionResolver：Skills 与个人/项目指令，不直接执行 shell。
- ModelGateway / McpManager / ProcessRunner：外部协议适配和生命周期。
- Persistence：SessionStore 协调 JSONL 追加、artifact 和短事务投影，类型化 ORM 承载查询状态；ProjectionMaintenance 负责离线重建、删除与崩溃恢复。当前没有为同一调用额外建立透传 Repository / UnitOfWork 包装。
- Telemetry：诊断、指标与 Trace；不能被当作恢复依据。
- bootstrap：配置加载并显式组装依赖。内核不导入 Textual，不直接读取环境变量。

按层建目录、层内按业务组织。接口只为真实替换点和测试边界建立，不为每个函数套抽象类。

~~~text
src/agent_client/
  domain/          会话、运行、消息、工具、配置快照、事件、错误
  application/     session、runtime、context、skills、permissions、recovery
  infrastructure/
    models/        Responses 适配
    mcp/           连接、发现、调用、认证引用
    workspace/     rg、文件、补丁、进程与锁
    persistence/   JSONL writer、投影、ORM、Repository、事务、ArtifactStore
    observability/
  presentation/
    tui/
    cli/
  prompts/
  config.py
  bootstrap.py
migrations/
tests/
docs/
  DESIGN.md
  USER_GUIDE.md
~~~

## 4 会话模型与执行循环

### 4.1 标识和状态

Session 是可恢复的对话；Run 是一条用户请求的执行；ModelStep 是一次模型调用；ToolCall 是一个已确定参数的动作。分别使用 session_id、run_id、step_id、call_id。新增命令携带 command_id，重发同一 command_id 不重复入队。

Run 状态：QUEUED → RUNNING，期间可进入 WAITING_USER、COMPACTING、CANCELLING；最终为 COMPLETED、PARTIAL、FAILED、CANCELLED 或 INTERRUPTED。状态转换由应用服务执行并持久化。stop_reason 独立记录正常结束、预算耗尽、需要输入、拒绝、截断、网络失败、结果未知等原因。

ToolCall 状态：PROPOSED → WAITING_APPROVAL（如需要）→ READY → DISPATCHING → RUNNING → SUCCEEDED / FAILED / CANCELLED / UNKNOWN。DISPATCHING 是已提交执行意图、可能已经触发副作用的边界，崩溃后不可视为尚未执行。拒绝审批记为 DENIED。

同一个 Session 一次只有一个活动 Run。同一工作区不同会话的写工具与命令还需要共享的工作区执行锁；读取可并行。这个锁只能协调本 client，不能阻止编辑器或其他程序修改文件。

### 4.2 一次运行

1. 输入落盘后才向 TUI 确认已接收。快照记录工作区、模型、指令、Skills、工具目录和权限版本。
2. 加载已提交历史与最新压缩检查点，校验模型消息结构。
3. 在模型请求前完成 token 预算检查，必要时压缩。
4. 提交 ModelRequestStarted，流式调用模型；文本 delta 可以立即展示，但不是已完成模型消息。
5. 收到完整响应并校验成功后，提交原生响应 items、工具调用和用量。
6. 对完整工具批次做参数与权限校验；独立只读工具受并发限制执行，写工具和 shell 默认串行。
7. 每个工具结果独立提交。下一次模型请求前，按模型原调用顺序组装完整结果批次，不能按完成先后打乱历史。
8. 无工具调用时，依据响应结束原因返回结果；工具仍在运行或响应截断时不得宣称完成。未知副作用保留明确的风险说明和执行限制，不能伪装成成功。
9. 提交 RunFinished 后通知 TUI，随后才可处理下一条队列输入。

流式参数未闭合、JSON 未校验或模型 step 未完整提交时绝不执行工具。结构错误反馈模型修正；内部不变量损坏明确失败。COMPLETED 只表示本次运行正常交付，不是对代码质量的证明；最终回答应引用已取得的测试结果。

### 4.3 异步纪律

模型、MCP 与进程管道使用异步 API。文件大读写、hash、归档、阻塞系统调用放在线程执行；不能仅给同步函数加 async 关键字。线程里的文件替换不能被假装取消：进入替换临界区后等待其完成并记录结果。

工作任务不直接修改 messages。Session 所有者按序应用事件；数据库写事务短且有界，绝不在事务里 await 网络调用。每个并发 task 使用自己的 AsyncSession [D4]。结构化任务组负责整体取消，可恢复的工具异常转为 ToolResult，不意外取消其他独立只读工具。

## 5 TUI 与交互协议

底部 ContextMeter 仅显示上次完整响应的 input_tokens，标记 last input；正文 delta、工具完成和请求开始不刷新显示值。没有服务端计数或压缩完成后显示 unavailable，恢复会话从当前压缩边界后的最后完整响应读取实测输入用量。内部发送预算和压缩触发仍使用独立的 ContextManager，不把停留在上次请求的显示值当成当前请求预算。

Windows 剪贴板在工作线程使用 CF_UNICODETEXT 读写，复制采用隐藏的 message-only window 作为 owner；不在事件循环执行等待剪贴板锁或系统调用。Ctrl+C/另选 Ctrl+Shift+C 复制输入或正文选区，无选区不退出；Ctrl+V 与输入框右键替换当前输入选区，多行文字不提交。终端已有的 bracketed paste 事件保留，收到该事件不再次读取系统剪贴板。参考已锁定 Textual 8.2.8 的 _text_area.py 与 app.py，并区分 Textual 内部 clipboard 与 Windows 系统剪贴板。

WorkIndicator 以 4Hz 更新 `· ✧ ✦ ✧` 与 elapsed；等待模型、thinking、执行工具时工作，idle 与审批等待时停止。减少动画模式固定符号，不通过伪造 reasoning 文本表达活动。

采用对话主栏、固定输入区和简短活动状态。主栏在内部以用户 command_id 分组，以 run_id 路由执行事件；每次模型 step_id 对应独立回复，工具以 call_id 原位更新。界面不显示 Task/Agent 编号、角色标题和任务外框，以带底色的 `›` 用户输入、`•` 回复标记和留白建立层次。正文保持 Markdown，工具状态区分运行、成功、失败和结果未知。默认不显示 ID、逐次请求 token 明细或原始事件；诊断信息放在 /status。当前活动显示等待模型、思考、生成回复、运行工具、等待审批或压缩上下文。终端较窄时保持单栏。

流式正文与可读 reasoning 采用每个回复独立的串行呈现任务。delta 回调直接更新目标文本，不等待绘制；呈现任务最多每秒推进 60 帧，根据积压字符数、剩余呈现时间和上一帧实际渲染耗时决定片段大小；一次待呈现批次以约 300ms 为追赶目标，重复或增长的目标不延长当前期限，渲染偏慢时减少中间帧，避免数秒播放积压。该期限限制人为等待，不保证慢终端的实际绘制完成时间。不按句子、段落或换行等待文本到齐。正文只向 `Markdown.append` 追加新增片段，非前缀修正才 `update`；reasoning 只更新变化的前缀。已完成 Markdown 块保持组件身份，中文和已收到的组合 emoji 避免在片段边界截断。恢复历史直接显示全文，取消时立即追平已收到内容，卸载时取消后台呈现任务。跟随底部使用 Textual anchor，用户向上滚动时释放跟随。依据锁定 Textual 8.2.8 的 `widgets/_markdown.py`、`widget.py` 核对 append、锁和 anchor 行为。

节奏控制参考固定 Codex commit `19b7bffd7bd5c325a45b91111ce64c85610b90ba` 的 `codex-rs/tui/src/streaming/chunking.rs`：正常每 tick 呈现一行，积压达到 8 行或最早内容等待 120ms 时进入追赶模式，300ms 为严重积压阈值。本项目采用短文本片段及有界追赶时间，适配 Textual 的 Markdown 增量渲染；不复制 Codex 的整行队列和滞回状态机。

reasoning 使用独立的 ReasoningBlock（item_id、index、channel、text）贯通 Provider、Runtime 和 TUI。接收 summary/text delta 与 done，完成事件按原生 reasoning item 补齐缺失后缀并校验一致性；最终块与 ModelResponse 一同持久化。请求 reasoning.summary=auto，effort=none 时省略；服务端没有返回可读内容时不构造文本。同一 item 优先展示 summary，使用独立浅灰 Static，不混入回答正文，也不显示 encrypted_content。取消时刷新已接收的临时内容，未完成流不伪装成已持久化完整回复。高频 reasoning delta 与正文 delta 均不逐片写诊断日志。参考固定 Codex commit 的 `core/src/client.rs` build_reasoning 及 `core/tests/common/responses.rs` reasoning fixtures；参数支持仍取决于所选模型与服务端。

恢复按已校验日志重建最近二十条用户输入对应的完整任务分组，并展示待处理输入；不调用模型或执行工具。diff 新增绿、删除红并带行号，摘要最多十二行；工具详情默认折叠，展开预览最多一百二十行且最多一万二千字符。完整历史保存在 JSONL，完整输出通过 artifact 按范围读取；当前没有全历史滚动分页界面。

~~~text
工作区 D:/code/example   模型 …   会话 修复登录问题
────────────────────────────────────────────────
你：检查登录接口为何返回 500
Agent：先检查接口实现和失败测试。
▸ search_text   login                    完成
▾ run_command   pytest tests/auth        运行中
  ...输出...
────────────────────────────────────────────────
多行输入；输入中的文本在刷新、工具输出到来时不丢失
执行中 · 运行工具 · Esc 停止
~~~

交互契约：

- 工具详情的折叠标题移除组件默认上边框和垂直内边距，正文紧接标题。标题左键切换展开状态，展开正文内单击左键收起；右键与拖动文本选择不触发折叠。长输出收起后，只将该工具标题滚动至可见区域，不跳到对话末尾。该操作只影响展示状态，不改变工具执行结果。
- Enter 提交，Shift+Enter 换行，Ctrl+J 为换行备用键；保留 Ctrl+Enter 提交。输入框从一行开始，按文本视觉行增长到八行，之后在框内滚动。没有 Send、Cancel 或审批按钮。输入绑定仅在输入框聚焦时生效；审批列表聚焦时 Enter 确认默认 Deny。
- 输入以 `/` 开头且未进入参数时，展示带说明和参数提示的命令列表。按命令名做大小写无关的模糊匹配，前缀优先；上下键选择，Tab 或 Enter 补全，完整命令 Enter 直接执行。补全不调用模型、不执行命令；无匹配明确提示。Esc 优先关闭候选，下一次 Esc 才取消运行。候选与输入框在窄屏中保持可用。
- Textual Pilot 覆盖实际键盘事件、带换行的粘贴、命令筛选和补全、排队、审批与窄屏状态；真实 Windows 终端的 IME 行为另行人工验收。
- Windows 使用应用自有 `WindowsInputDriver`，运行期间启用 Win32 input mode（9001），退出时关闭。将携带修饰键的 Win32 records 转换后交给 Textual 的 XTermParser，保留鼠标、焦点、粘贴和 UTF-16 分片；输入线程失败时显示错误并退出。Textual 固定为 8.2.8，升级时必须复核内部 driver/parser 契约。原版 `drivers/win32.py` 在 VT 模式拼接 UnicodeChar，不能可靠区分 Enter 的修饰键；不能用 Pilot 事件模拟通过来证明真实终端兼容。验收使用 Windows 伪终端注入 Win32 records 检查 Enter、Shift+Enter 和命令补全，物理键盘及 IME 使用真实终端交互验收。不支持增强输入协议的终端可用 Ctrl+J 换行。
- 参考 Codex 固定 commit `19b7bffd7bd5c325a45b91111ce64c85610b90ba` 的 `codex-rs/tui/src/bottom_pane/chat_composer.rs`：普通 Enter 提交，并单独处理 Windows 粘贴输入。本项目沿用 Enter 提交语义，通过 Textual bracketed paste 事件保留粘贴块，不将其中换行解释为提交；Win32 协议依据微软 [ConPTY keyboard handling specification](https://github.com/microsoft/terminal/blob/main/doc/specs/%234999%20-%20Improved%20keyboard%20handling%20in%20Conpty.md)。
- Esc 请求取消当前 Run；输入草稿和历史保留。退出应用走取消、持久化、子进程清理和数据库关闭流程。
- Enter 在活动 Run 中提交 steer，Tab 显式排队；菜单可见时 Tab 用于补全。恢复应用不自动消费旧队列，/continue 优先使用已持久化输入。
- 权限请求在输入框上方显示，默认 Deny，上下键选择、Enter 确认、Esc 拒绝；审批期间 composer 禁用。完整命令、路径和参数可滚动查看，绑定 request_id、call_id。MCP elicitation、sampling 和 roots 回调尚未实现，不向服务端宣告支持。
- /permissions 在输入框上方打开模式列表，上下键选择、Enter 确认、Esc 关闭；/permissions MODE 直接设置。模式只对当前进程生效，不写回配置，运行期间不能切换。持久字段为 runtime.approval_mode，CLI 覆盖为 --approval-mode。
- 当前命令包括 /new、/resume、/continue、/sessions、/login、/logout、/model、/reasoning、/permissions、/skills、/mcp、/compact、/status、/config、/resolve。配置文件变更需要重新启动应用；模型切换只在运行之间进行。
- 模型切换只能发生在 Run 之间；不兼容的原生 items 不能直接跨供应商发送，必须明确建立新上下文或新会话。
- 文本流与工具 stdout 分离；终端控制字符经过处理，工具输出不得直接改变终端状态。

Enter 在活动 run 中提交 steer，Tab 显式排队，命令菜单可见时 Tab 仍用于补全。steer 写入带 target_run_id 的 PendingInput，持久化后确认接收；完整模型/工具批次之间追加 UserMessage，随后同一 run 继续。不会取消已执行工具或拆开调用与结果。最终回答后原子检查待接收输入，再关闭 steer 接收边界；边界关闭后的输入排入下一 run。失败或取消后未消费消息保留，/continue 读取日志继续，不重放已完成工具。

参考固定 Codex commit `19b7bffd7bd5c325a45b91111ce64c85610b90ba`：`codex-rs/tui/src/bottom_pane/chat_composer.rs:3492` 区分提交与排队，`codex-rs/core/src/session/input_queue.rs:389` 原子提取输入，`codex-rs/core/src/tasks/regular.rs:115` 在任务正常结束前检查 pending。TUI 按消费回执切换后续输出所属用户消息，恢复同一 run 的多条用户输入时保持顺序。

TUI 发送 SubmitMessage、CancelRun、RespondToInteraction 等命令。Runtime 发出 TextDelta、ToolStarted、ToolOutputChunk、ToolFinished、InteractionRequested、CompactionStarted、RunFinished 等事件。

持久化事件携带递增 seq；高频 delta 仅暂态显示，TUI 立即投递最新文本目标，由独立呈现任务合并与绘制，不在 delta 回调里等待动画。完成及切换回复时等待当前呈现任务追平，取消时关闭平滑等待。其他事件回调仍按序 await，关键执行状态必须先完成持久化再发送展示事件。节点和正文长度有界，恢复时使用已校验的日志和投影。每条事件带 run_id，取消后的晚到输出不能写进下一次 Run。全历史分页属于后续性能改进。

对话展示参考固定源码：Codex commit `19b7bffd7bd5c325a45b91111ce64c85610b90ba` 的 [history_cell/messages.rs](https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/tui/src/history_cell/messages.rs) 使用独立用户提示底色和 assistant 首段标记，流式续段属于同一消息；[chatwidget/completion.rs](https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/tui/src/chatwidget/completion.rs) 按 turn.id 去重完成信息；[status_indicator_widget.rs](https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/tui/src/status_indicator_widget.rs) 在 composer 上方显示活动；[diff_render.rs](https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/tui/src/diff_render.rs) 区分红绿变更与行号。

pi commit `ce950d78f424dcaf9f5d6a03ce80ab141130eb1d` 的 [interactive-mode.ts](https://github.com/badlogic/pi-mono/blob/ce950d78f424dcaf9f5d6a03ce80ab141130eb1d/packages/coding-agent/src/modes/interactive/interactive-mode.ts) 按 message_start/update/end 更新单条 assistant 组件，工具按 toolCallId 更新，agent_end 才移除 working 状态；[agent-loop.ts](https://github.com/badlogic/pi-mono/blob/ce950d78f424dcaf9f5d6a03ce80ab141130eb1d/packages/agent/src/agent-loop.ts) 的 turn_end 是一次模型响应与工具结果周期，不能用作整条用户请求的结束。[theme/dark.json](https://github.com/badlogic/pi-mono/blob/ce950d78f424dcaf9f5d6a03ce80ab141130eb1d/packages/coding-agent/src/modes/interactive/theme/dark.json) 将用户输入、工具待执行/成功/失败、diff 设为独立颜色语义。本项目沿用语义分层，以自己的 command_id/run_id/step_id/call_id 契约实现 Task 和 Agent 分组，并选择更简短的默认展示，不复制两者的事件名称或完整界面。

## 6 配置与个人指令

ModelConfig 的 context_window 默认值为 256000，用于本地预算；实际容量需要按所选服务配置。压缩触发与目标见第 11 节。

显式 `openai_chat_completions` provider，仅允许 `api_key` 认证；bootstrap 按 provider 组装 ChatCompletionsGateway 或 ResponsesGateway，不透明回退。兼容配置见 config.openai-compatible.example.toml；DeepSeek 配置见 config.deepseek.example.toml，使用 deepseek-flash 和 DEEPSEEK_API_KEY 引用。密钥从所配置的环境变量读取，不写入配置或会话历史。

日常入口为 agent-client，默认读取用户目录 ~/.agent-client/config.toml；AGENT_HOME 或 --home 显式覆盖用户数据目录。用户在默认配置中修改 provider、auth_mode、base_url、model、api_key_env 等标识，重启后生效。--config 仅为临时覆盖，不作为切换服务的必要步骤，不新增进程内 provider 菜单。认证模块按配置处理登录，凭据单独保存；配置不保存令牌或登录成功布尔值，也不由登录流程自动覆盖用户选择的端点与模型。

ProviderAccess 根据当前 auth_mode 路由账户操作：API key 的 status 仅验证变量存在，login 通过 /models 验证连接、models 返回服务目录，不启动浏览器、不修改 ChatGPT 凭据。API key 模式 logout 不撤销 ChatGPT 账户，提示移除相应变量或切换配置。RunStarted 保存 provider/base_url/auth_mode；续作与执行前比较历史身份，变化立即拒绝并要求新会话。

旧 RunStarted 缺少 base_url 或 auth_mode 时，其未知绑定仅允许原默认官方 OpenAI Responses + ChatGPT 组合（https://api.openai.com/v1）。其他组合在续作或压缩前拒绝，要求新会话，不能把缺失字段解释为任意 endpoint 或认证方式通用。

ModelConfig.reasoning_levels 为可选强度的显式非空、无重复集合，当前 reasoning_effort 必须在其中；未配置时沿用 provider 的默认选项。/reasoning 在 composer 上方显示菜单，上下键选择、Enter 确认，/reasoning LEVEL 和 --reasoning-effort 调用同一配置选择契约；进程内设置不写回 TOML，活动运行期间不能切换。DeepSeek 示例允许 none/high/max：none 对显式 thinking 协议发送 disabled，非 none 启用 thinking 并发送 effort。通用 chat 默认 chat_reasoning=default、chat_send_reasoning_effort=false，不发送额外 thinking/effort；显式选择会启用 effort，默认 thinking 协议不能自行解释 none。chat_stream_usage 为独立显式开关。

Chat Completions 的 reasoning_effort=none 与 chat_reasoning=default 组合在配置构造边界立即拒绝；不能通过直接修改 TOML 绕过菜单的 none 校验。服务支持显式 thinking 开关时，须配置对应 enabled/disabled 协议后再选择 none。

ChatCompletionsGateway 将 Responses 形式的运行上下文转换为 messages，保留工具调用身份、跨工具轮次的 reasoning_content 和工具结果；只有完整响应及调用参数校验后才执行工具。推理增量与正文分开，usage 按实际响应记录，不把缺失字段当零。

应用暂用包名 agent_client，应用数据根以 AGENT_HOME 配置，默认用户主目录下 .agent-client；不读取或改写 Codex 的认证与状态库。允许显式引用用户现有 Skill 目录。

当前配置优先级为应用默认值 < 个人 TOML 或显式 --config 文件 < CLI 支持的显式参数。AGENT_HOME 控制数据目录，其他环境变量用于凭据引用。当前不自动加载项目 TOML 或任意环境变量配置；项目文本指令不能提升程序权限、改变凭据来源或增加 MCP 可执行命令。显式配置文件缺失或字段非法立即失败。

当前配置结构示例：

~~~toml
[model]
provider = "openai_responses"
model = "gpt-6.1-sol"
auth_mode = "chatgpt"

[skills]
roots = ["~/my-skills"]
catalog_token_budget = 2000

[context]
soft_ratio = 0.95
target_ratio = 0.25
strategy = "summary"

[runtime]
max_model_steps = 40
max_tool_calls = 120
max_parallel_reads = 4
deadline_seconds = 1800

[mcp.servers.documents]
transport = "stdio"
command = "user-installed-server"
args = []
required = false
~~~

数值是本项目初始默认建议，不是 Codex 默认值或实际性能结论。解析后校验范围；endpoint、模型窗口、输出上限和必要凭据缺失时尽早报错。密钥只保存环境变量名或 OS 凭据引用，不进配置快照、日志或版本库。

个人 AGENTS.md 与项目根 AGENTS.md 构成启动指令；访问更深目录前解析适用的目录规则，标明来源与作用域。当前由用户显式指定工作区，写入和命令权限分别审批，不另设项目配置的信任数据库。新加载的目录规则随工具结果进入下一次模型请求；写操作不得越过尚未读取的适用规则。

指令内容、Skill 正文和工具 schema 记录内容 hash 与受保护的不可变快照。运行期间检测到变更时在下一安全边界生成新版本，不悄悄替换正在使用的规则。用户当前明确指令优先于 Skill 和项目偏好；这些文本不能覆盖程序执行的权限限制。

## 7 个人 Skills

扫描配置根目录的 SKILL.md，解析 name、description、正文路径及可选元数据。设置扫描深度、文件大小与条目数预算，忽略无关大目录。不能递归扫描整个用户主目录。

每个 Skill 的身份为 root_alias + relative_path，显示名称不作为唯一主键。名称冲突显示来源并要求显式选择，不任意覆盖。目录摘要按稳定 ID 排序；默认只注入名称、描述、来源和加载方式。

提供：

- search_skills(query)：对目录元数据做确定性关键词检索，返回候选。
- load_skill(skill_id)：读取并固定本次内容版本，向模型提供完整正文。
- read_skill_resource(skill_id, relative_path)：读取该 Skill 根下的引用文件。

显式指定 Skill 优先加载；隐式选择由任务与 description 判断，不仅凭关键词自动执行。重复加载相同版本返回已加载标记；预算不足时目录给出明确省略数量并通过 search_skills 取回。

Skill 是指令与资源，不是权限凭证。执行其脚本仍经过普通工具链。相对路径相对 Skill 根解析并检查解析后的范围，处理符号链接与 Windows junction。跨出根目录的资源要经过独立访问判断。损坏元数据在 /skills 中显示具体错误；用户明确请求的 Skill 不能静默跳过。

指令来源标识与版本在压缩后保留。必要约束可重新从冻结的 Skill 快照加载，不依赖摘要完整记忆每项规则。

## 8 MCP 接入与工具目录

MCP 配置环境引用按 Process、Windows User、Windows Machine 优先级解析；只有前一来源缺失才尝试后一来源，显式空值不回退。注册表只查询配置引用的名称，通过 asyncio.to_thread 读取，解析值用 SecretStr 保留于连接生命周期，并用于目录及结果脱敏；不写入配置、会话和日志。连接错误中只有已知缺失变量异常展示变量名，其他错误不输出可能含凭据的异常正文。启动时展示连接状态，运行前缀包含服务连接目录；search_mcp_tools 返回具名 tools、servers 和查询提示，空结果不能被解释成没有配置 MCP。短关键词或空查询用于 schema 延迟发现，连接失败不得静默掩盖为纯空数组。

使用 SDK v2.3.0 的高层异步 Client。该版本 mode=auto 先探测现代协议，旧 server 则由 SDK 进入旧握手；本项目默认采用这个标准协商并在 /mcp 展示实际 protocol_version [M1]。这不是应用层维护两套兼容实现。当前没有最低协议版本的额外配置项，不能将完成连接描述为使用某个未经观测的版本。

支持 stdio 与 Streamable HTTP。McpManager 管理连接、timeout、能力和认证引用；上下文管理器的创建与清理由同一所有者任务负责。恢复进程后重建连接，不反序列化网络连接或沿用失效 session 句柄。

默认采用稳定的延迟暴露入口：

- search_mcp_tools(query, server)：返回工具 ID、说明、完整输入 schema 与 schema_hash。
- call_mcp_tool(tool_id, schema_hash, arguments)：对冻结定义校验参数，通过统一执行器调用。
- MCP resources 使用明确的 list/read 工具；prompts 仅在用户选择或任务需要时加载，不自动变成高优先级指令。

这样 server 增加数百个工具时，不必每轮修改模型原生 tools 列表。代价是多一次发现调用；本阶段接受这个代价。后续可依据实际指标评估小工具集直接暴露，不同时维护两套默认行为。

目录记录 server_id、原始工具名、描述、schema、schema_hash、发现时间和权限标记，处理分页。ID 使用稳定映射，不依赖字符串拆分恢复原始名称。同名工具不会冲突。

收到目录变更通知或 TTL 到期后，在安全边界更新。已发出的调用绑定旧 schema_hash；若服务不再接受该版本，返回定义过期，要求重新发现，不静默改参数重试。

required server 启动失败阻止相关运行；optional server 失败显示不可用，不能导致 coding 工具一起失效。重连和重试受总体预算限制。readOnlyHint 用于已配置服务的执行风险分类，不替代 REMOTE 权限审批，不触发自动重放；未明确只读的调用按有副作用处理，并发与重试仍由本地工具策略决定。

保留 structuredContent、content、isError 与必要协议元数据。不能简单转成 str。首版模型不支持的媒体以明确类型和资源引用返回，不伪装已读取。用户输入请求与现代多轮请求进入同一交互状态机；无实现的可选能力不向 server 宣称支持。认证过期显示 NEEDS_AUTH，不将其当作空查询结果。

已配置 MCP 服务的只读声明进入具名工具契约与 schema fingerprint；权限仍保留 REMOTE 审批，声明不扩大写入授权。查询超时记 FAILED，未明确只读的远端操作仍保守记 UNKNOWN。未解决的 UNKNOWN 保留在日志中并向模型说明，新用户输入与读取可继续，副作用执行由 ToolService 阻止，不再在 Run 入口阻断整个会话。

连接 owner 终止时使在途和等待请求明确失败。请求超时或取消会清理所属连接，下一次独立请求可重新初始化；不得重放此前调用。界面将已提出但未派发的工具标为 queued，收到 TOOL_DISPATCHING 后才显示 working。

存在未知结果时仍允许压缩，但保留相应调用与结果的完整原生分组，执行限制始终由原始日志计算。只读/副作用分类在派发事实中持久化，重启后的恢复不因丢失临时内存而把新只读查询误记为未知；旧记录缺失分类时保持保守。当前批次发生未知后，随后有副作用动作立即拒绝，避免等整批结束才发现风险。

## 9 本地检索与编码工具

### 9.1 检索分工

- list_files：目录结构与 glob 过滤，以 rg --files 为主要文件列表来源。
- search_text：正文关键词、固定字符串或显式正则，使用 rg 的结构化输出。
- read_file：按行读取，带相对路径、行号、编码信息与内容版本。
- TUI 的 @文件补全：路径候选和模糊排序，不能冒充正文语义搜索。

所有 rg 调用使用参数数组，查询与路径作为数据传入，避免拼 shell 字符串。默认尊重 ignore 规则，不默认扫描 .git、依赖缓存、二进制和隐藏凭据；用户可显式调整可见范围。正则、结果条数、输出字节、时限和并发均有界。

区分 rg 退出码 0 的命中、1 的无匹配与错误退出。输出明确标记 complete / truncated / timed_out；达到上限即停止采集并要求缩小范围，不能将截断说成“搜完没有”。默认按路径与行号提供确定性排序；多次搜索跨文件变化不能宣称同一快照。

首版不引入向量检索。coding-agent 的主路径是先定位文件和符号，再读取有限上下文；需要语义检索时可通过 MCP 接入。rg 缺失时启动诊断给出安装指引，不在 Windows 静默退化成语义不一致的 grep。

### 9.2 修改与进程工具

apply_patch 在准备阶段记录预期原文 hash、目标 patch 和预期结果 hash。实际写前重新核验；发生并发修改则报冲突，不猜测覆盖。单文件以临时文件加替换完成，保留原文件必要属性；多文件 patch 不宣称跨文件原子，逐文件记录结果并在恢复时核对。write_file 同样记录创建/覆盖前置条件。

run_command 显式指定工作目录与 shell，首版 Windows 使用配置的 PowerShell，POSIX 使用配置的 shell。固定命令如 rg 使用直接进程执行。stdout/stderr 分开收集，保留退出码、信号、时限与输出引用。stdin 默认关闭，避免隐式等待；首版不支持需要真实交互终端的程序。

长命令可返回 process_handle，再使用 poll_command、stop_command 获取状态或停止。运行时仍持有进程任务；不能遗忘后台命令后直接完成 Run。handle 只在所属 runtime_instance 有效，重启后不得复用 PID 冒充原进程。

取消需要停止进程树并收集退出结果：Windows 使用受控 Job Object/等价执行器，POSIX 使用进程组。接入前验证实际平台行为；无法证明已停止时记为 UNKNOWN，不能仅因 asyncio task 已取消就显示“命令停止”。

区分取消请求与实际 terminal 结果：本地执行器确认终止后保留 stdout、stderr 和 exit code，ToolService/Runtime 等待结果及日志持久化屏障，记录 CANCELLED；若取消到达时已完成，则记录真实 SUCCEEDED/FAILED。取消不回滚此前副作用，也不能因正常取消而误记 UNKNOWN。客户端崩溃导致派发结果无法确认、远端副作用请求被中断或无法确认终止时，仍走 UNKNOWN 恢复屏障，不自动重放。会话若未保留终止事实，不能自动修改其历史 UNKNOWN。

实现参考固定 Codex commit `19b7bffd7bd5c325a45b91111ce64c85610b90ba` 的 [core/src/tools/parallel.rs:262](https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/src/tools/parallel.rs#L262)：取消时，对 finishes_on_cancellation、已 terminal 或已完成的 dispatch 等待真实结果；其他分支 abort 后等待任务结束。本项目采用本地进程终止确认与事实持久化屏障，不将该参考直接等同于本项目权限和恢复策略。[Claude Code 官方交互文档](https://code.claude.com/docs/en/interactive-mode) 明确 Esc 中断当前回复或工具且保留已做工作；该交互文档只作为用户行为参考。

每次工具执行均记录副作用类别：READ、WORKSPACE_WRITE、PROCESS、REMOTE_WRITE。READ 可受控并发；其他默认串行。补丁修改与 shell 共享工作区执行锁。

## 10 前缀缓存设计

### 10.1 稳定结构

缓存是模型服务端的前缀复用，不能通过在本地缓存上一次答案替代。实际能否命中还取决于模型、服务端策略和保留时间；本项目优化可控的输入稳定性，不承诺命中率 [D1]。

逻辑请求顺序：

1. 固定运行规则及稳定的本地工具定义。
2. 个人偏好、已信任项目规则和当前冻结的 Skill 目录。
3. 会话初始说明或已提交的压缩检查点。
4. 按顺序追加的用户输入、模型响应和工具结果。

模型 API 对 instructions、tools 与 input 的实际排列由适配器负责并通过请求快照验证，不能假设不同 API 的字段排列相同。不每轮重写 system prompt，不把时间、剩余 token、运行进度或随机 ID 注入稳定头部；变化事实以新增上下文 item 表达。

固定工具排序、schema 序列化、目录排序、换行与编码。历史响应中的原生内容、调用 ID、必要 reasoning/encrypted items 原样保留，不在恢复时重新生成随机 ID。Provider opaque items 只回传所属适配器，不当作用户可读推理日志。

### 10.2 版本与失效

当前 PrefixSnapshot 对 instructions、排序后的工具定义及序列化版本生成 prefix_revision；Skill 目录作为 instructions 的一部分参与计算。RunStarted 冻结实际指令与工具定义，ModelRequestStarted 保存前缀版本、活动上下文 hash 和 epoch。模型及推理配置由当前配置显式决定；尚未实现前缀变更位置分析或完整配置快照的独立索引。

Runtime 生成稳定、非敏感的会话 cache key，不把 run_id 或时间戳做成每轮不同的 key。当前仅 API key 模式向 Responses 发送 prompt_cache_key 和 max_output_tokens；订阅模式未核实这些字段可用，因此不发送。订阅模式仍保持输入前缀稳定并记录服务返回的缓存用量，不能据此宣称已验证命中。缓存 key、保留期限与恢复 checkpoint 是三个不同概念。

同一上下文窗口内，工具结果一旦进入模型历史不反复改写。长输出应在首次入模前完成投影；压缩则形成明确的新 context_epoch。加载 Skill 正文和 MCP 检索结果追加到历史尾部，不重排已有前缀。用户更新指令时以正确性优先，允许有记录的缓存失效。

### 10.3 观测与验收

每次模型请求记录输入/输出 token、服务端返回的 cached_input_tokens、可用时的 cache_write_tokens、首 token 时延、总时延、prefix_revision 和 context_epoch。服务未返回的字段为 unavailable，不能按 0 统计。费用仅在配置了已核实费率时计算并标注估计，不固化单一模型计价。

本地验收检查相邻请求的稳定前缀字节是否一致，以及工具/技能顺序变化是否有版本原因。真实 API 验收观察重复前缀用量，不设“缓存必须命中”的不可靠 CI 断言。

## 11 上下文管理与压缩

### 11.1 完整历史与活动窗口分离

JSONL 保存完整的已提交会话事实，模型消息正文可内联或引用不可变 artifact；SQLite 保存消息位置、会话状态与查询投影。模型仅收到 ContextManager 构造的活动窗口。压缩不删除完整历史，不改变已执行工具结果，也不影响 TUI 查看原文。

大输出首次返回时保存完整不可变内容，模型只收到受预算限制的摘要视图、总长度、截断标记和 artifact_id。read_tool_output 按范围取回，校验 artifact 的所属会话和可见性。搜索片段、错误末尾和引用位置有明确保留规则，不能一律截取开头。

### 11.2 触发与预算

工具结果投影集中在 application/tool_output.py：完整结果及不可变附件与模型活动窗口分离。每条入模结果的序列化文本统一受 context.tool_output_characters 限制，默认 16000；已有 artifact_id 也不能跳过限制。保留首尾、原长度、状态和读取引用。MCP 文本与 structured_content 仅在 JSON 语义完全相同时去重，其他不同内容保留；二进制媒体保存在完整记录，不把 base64 当正文投入文本上下文。调用与结果始终按 call_id 配对，不把工具输出提升为用户或系统指令。

参考 Codex 固定 commit 19b7bffd7bd5c325a45b91111ce64c85610b90ba，codex-rs/core/src/context_manager/history.rs 的 record_annotated_items 与 record_item_with_metadata 在保留完整 rollout 的同时，对活动历史里的工具结果应用 TruncationPolicy。这里采用完整事实与有界入模视图分离，首版以可配置序列化字符上限执行，不宣称等价于精确 token tokenizer。

令 W 为模型上下文容量，O 为本次输出/推理预留，S 为估算安全余量，H = W - O - S 为可用输入预算。容量与计量口径来自已验证模型配置，不凭名称猜测。

- 每次模型调用前计算整个请求的输入估计，包含工具 schema、指令和媒体。
- 达到 soft_ratio × W 时在安全边界启动压缩，默认 soft_ratio=0.95；与绝对窗口大小无关。
- 压缩目标不超过 target_ratio × W，默认 target_ratio=0.25；256k 窗口压缩后最多 64k，包含固定指令、工具定义、摘要和受保护尾部。
- 即将超过 H 时必须压缩或明确终止；缓存 token 同样占上下文，不能扣掉。
- 用户可使用 /compact；模型可提出压缩请求，仍由 Runtime 检查边界。
- 服务报告 context overflow 时允许一次有记录的补救；失败后停止，不无限重试。
- 未完成的工具调用批次、待裁决副作用和未提交模型 step 不允许被切断。

内部预算以最近一次服务端计数为基准，只估算之后新增的上下文；首次请求或无法复用计数时使用保守估算。界面仅显示上次服务端 input_tokens，不再显示该内部增量估算，两者明确分离。字符数不直接当成 token。若固定前缀本身超预算，报告配置问题，不能通过压缩历史掩盖。

默认 W=256000、O=8192、安全余量 max(1024, W×0.01)=2560，可用输入预算 H=245248，自动触发点为 W×0.95=243200。显式配置造成输出预留先于 95% 耗尽输入空间时仍执行硬预算保护，不能发出已知超限请求。服务端 usage 是校准点，压缩或历史替换后基准重建；缓存命中不减少占用。

### 11.3 压缩策略

参考 Codex commit `19b7bffd7bd5c325a45b91111ce64c85610b90ba` 的 `codex-rs/core/src/compact.rs:372`：接收非空文本摘要并重建上下文。本项目采用相同文本契约，额外验证完整工具配对、检查点持久化和窗口预算。

摘要使用独立的 summary_max_output_tokens（默认 8192）与 summary_reasoning_effort（未配置时 Responses 使用 low，具有显式 thinking 开关的 Chat Completions 使用 none，其余使用当前模型档位）；Responses 与具有 thinking 开关的 Chat Completions 不继承普通回复的高推理档位。摘要响应未完成时保存诊断事实，保持原上下文；摘要以严格非空文本契约接收。空摘要、未完成响应、意外工具调用和未达到目标预算均不提交新窗口。

默认 strategy=summary：在单独、无工具的模型请求中生成由 ConversationSummary 校验的纯文本摘要，包含当前目标、用户限制、关键决定、已修改文件、验证结果与来源、未完成事项、有效 Skill 与证据引用。当前用户请求只保留一次，并保留最近完整模型/工具批次和全部未决尾部。摘要只作为上下文材料，不提升为新的系统授权。

ContextWindow 记录完整分组的 start/end 边界。一个 ModelResponseCommitted 的全部原生 output 与其按 call_id 顺序配对的结果构成一组，不能把 reasoning、并行调用和结果分别裁切。分组来自原始日志，压缩检查点保存同一组边界。因此即使只有一条用户请求，也能压缩长 Run 内已完成的旧步骤。含后台未终结进程句柄的完整组仍受保护，只有终态 poll 或已记录的进程处置结果才能解除保护；句柄集合按排序后的顺序序列化。旧检查点没有分组信息时，按完整用户交互保守分组，不猜测原生响应边界。

摘要请求保留当前请求和待压缩历史，最后追加明确的合成用户消息要求生成交接摘要，禁止继续回答历史用户消息。参考 Codex `compact.rs:123` 和 `compact.rs:270`，合成指令只用于本次摘要，不进入恢复检查点；其成本也计入预算。摘要输入按配置的完整窗口 W 判断，不额外扣除普通请求的输出预留与安全余量。首次尝试完整历史；估算超限或供应商明确报告上下文超限时，严格缩小当前批次。每批携带前批摘要、当前请求和合成总结指令，成功后推进历史游标；每个旧分组按顺序处理一次，工具调用和结果不得拆开。参考固定 Codex commit 的 `codex-rs/core/src/compact.rs:338`，其超限补救移除最早历史项；本项目采用完整分组分批总结，保留尚未总结的历史。单个不可拆分分组仍超限，或固定前缀及受保护尾部本身超过目标时，明确返回 context_budget 并保留原窗口。

压缩输出校验结构、预算、引用可访问性和工具配对；这只能发现结构问题，不能证明语义完全无损。关键指令由冻结原文快照重新构建，未决副作用由 JSONL 记录并投影到数据库，不能只靠摘要记忆。

当前实现 strategy=summary。配置 strategy=provider_native 会得到明确的不支持错误，不会静默切换策略。后续若核实模型原生 compact 能力，再按官方契约保存完整返回窗口，不自行只摘出摘要或 opaque item [D2]。

中间摘要只在当前压缩操作中使用。全部批次成功、原生调用配对完整、替换窗口满足 25% 目标且活动上下文未发生冲突后，才提交一次 checkpoint；后续批次失败或取消均保留原窗口。压缩不执行工具，UNKNOWN 对应的完整分组仍受保护，副作用限制始终根据原始日志计算。

压缩提交流程：

1. 记录 old_epoch、source_seq、输入摘要和待提交 checkpoint。
2. 无数据库事务地调用压缩模型。
3. 校验结果，确认活动上下文没有越过 source_seq 变化。
4. 先持久化完整 replacement_context 的 artifact，读回校验内容 hash、模型结构与分组，再追加一条 CompactionCommitted JSONL 记录，包含 artifact 引用、摘要元数据、source_seq 和新 epoch，等待 durability barrier。
5. SQLite 在单个事务中更新检查点投影、活动指针和投影游标；成功后发送 CompactionFinished。数据库更新失败按第 12 节追平，不重新调用压缩模型。

取消或崩溃后，以是否存在完整有效的 CompactionCommitted 记录决定使用哪个窗口：不存在则继续旧窗口，存在则从新窗口及 source_seq 之后的事件恢复，即使 SQLite 指针尚未更新。所有压缩均记录触发原因、前后 token、耗时、模型和策略版本。

## 12 JSONL 与 SQLite 持久化

### 12.1 分工与事实归属

按照用户决定，同时使用 JSONL 与 SQLite，借鉴 Codex 的 rollout 与状态存储分工 [C6][C7]。本项目进一步明确以下契约；不声称 Codex 的全部数据库都是可丢弃投影。

| 载体 | 保存内容 | 恢复中的地位 |
| --- | --- | --- |
| 会话 JSONL | 用户输入、完整模型响应、工具执行意图与结果、交互决定、运行状态变化、压缩检查点及配置快照引用 | 会话事实来源；只追加，恢复按顺序回放 |
| state.sqlite | 会话列表、队列和运行状态、工具状态、用量聚合、消息位置、检查点索引、投影游标 | JSONL 的查询与状态投影；丢失时可从会话记录重建 |
| artifacts/ | 大输出、模型原生内容、压缩窗口、指令与 Skill 快照、修改前后内容 | JSONL 引用的不可变内容；丢失正文不能仅靠数据库索引恢复 |
| 诊断日志 / Trace | 耗时、请求摘要、异常和运行指标 | 可独立轮转，不用于重建会话事实 |
| 个人配置与凭据存储 | 当前用户配置、授权来源和密钥引用 | 独立配置来源；凭据不放进会话记录 |

采用这种分工后，“数据库已经有一行”不能单独证明动作发生，“JSONL 没有结果”也不能证明外部动作未执行。所有会影响恢复的队列消费、审批决定、状态和配置版本都必须有对应会话记录，不允许只写 SQLite。

数据位于 AGENT_HOME，独立于被操作项目。目录形态为 sessions/<session_id>/rollout.jsonl、state.sqlite、artifacts/ 和 logs/。会话首条 SessionCreated 记录含恢复列表所需的身份与工作区元数据；数据库缺失时可以扫描会话目录发现记录。日志和数据库仅放本机磁盘，不支持共享网络盘上的多机协作。

跨层数据采用 domain 中的 Pydantic 契约，文件内部记录采用 dataclass。动态 MCP schema、参数和结构化响应只在协议边界封装为 ProtocolObject；固定字段、重复 JSON 键和非有限数在边界校验，业务状态不保存裸字典。原生 Responses 消息保留 phase、reasoning、annotations、logprobs 等具名字段；字段契约参考 openai-python 固定 commit `9301e319ea33ef28fba380f39a289dedc14652c1` 的 `responses/response_output_message.py` 与 `response_output_text.py`。

JSONL payload 使用版本 2；版本 1 的字符串结果和 MCP 服务目录仅在读取边界显式迁移。先按原始 wire 内容验证校验和，再转换当前契约；不重写历史，不重放工具。

### 12.2 JSONL 记录与持久化屏障

每个会话一条有序记录流，由持有会话 OS 锁的 RolloutWriter 独占追加。单条记录包含 log_format_version、session_id、seq、event_id、run_id、type、payload_version、payload、record_hash；seq 按会话递增，event_id 在首次提交前生成并在重试时复用。record_hash 基于约定的规范化编码计算，用于完整性检查，不等同于防篡改签名。

记录使用 UTF-8，每条以换行结束。一个需要整体提交的语义变化放进一条记录，例如 AssistantStepCommitted 同时包含完整响应与工具调用清单，RunStarted 同时表示输入消费与运行创建；大内容先写 artifact 后引用。不能依赖“连续写入多行天然原子”。

durability barrier 指 writer 完成写入、flush，并执行文件同步后才确认持久化；创建新文件和 artifact 时处理平台要求的目录/元数据同步。异步队列入队不等于持久化，Python 文件 flush 也不等于 fsync。实际断电保证受 OS 与磁盘影响，需要实测，不夸大为绝对不丢数据。

用户输入、完整模型响应、工具执行意图、工具终态、审批决定、压缩检查点和运行终态都经过屏障。高频文本 delta 与 stdout 展示可以合并，不逐字符刷盘。模型响应或工具意图未确认持久化时，不得启动新副作用。

追加失败或 ACK 丢失时，不能直接重复写一条新记录：先在同一会话锁内检查尾部 seq、event_id、hash，已存在则确认原记录；尾部不完整按修复流程处理。重启后完整、校验有效的记录视为已提交事实，即使旧进程未收到确认；command_id 和 event_id 去重避免重复受理。

### 12.3 写入顺序与 SQLite 投影

所有状态变化走同一个持久化协调器：

1. 核验当前状态与输入幂等标识，准备不可变记录；必要 artifact 先持久化。
2. 追加 JSONL，等待 durability barrier。
3. Projector 在短 SQLite 事务中应用记录，更新受影响表和 applied_seq / byte_offset / record_hash。
4. 数据库事务成功后更新内存状态并通知界面；工具执行意图还须完成本步才可真正发起动作。

JSONL 与 SQLite 之间没有跨存储事务。第 2 步成功而第 3 步失败时，JSONL 已经提交：停止发起新动作，修复或重新打开数据库，从旧游标幂等追平，不能撤销日志事实或重新执行对应工具。

Projector 按连续 seq 应用；重复 event_id 跳过，seq 跳跃或相同身份内容不一致报错。状态更新、索引写入和游标推进必须在同一数据库事务中，不能先前移游标再写状态。SQLite 不能出现领先于 JSONL 的有效投影；发现领先、hash 不符或 offset 无效时停止并重建受影响投影。

UI 可先显示暂态流式内容，但“已接收”“已完成”的确认只使用持久化协调器结果。投影故障显示已持久化但索引待恢复，不将已写入的用户消息误报为从未保存。

| 表 | 主要字段与约束 |
| --- | --- |
| sessions | id、workspace、title、status、context_epoch；seq、byte_offset、record_hash 同行保存投影游标 |
| event_index | session_id、seq、event_id、type、run_id、JSONL offset / length；(session_id, seq) 唯一 |
| runs | (id, session_id) 主键、状态、stop_reason |
| inputs | (command_id, session_id) 主键、消费 run_id、消费事件 seq |
| tool_calls | (call_id, session_id) 主键、工具名、状态、run_id |
| context_checkpoints | (event_id, session_id) 主键、epoch、source_seq、replacement_context artifact 引用 |

这是当前六张投影表；消息正文、模型步骤、审批、待处理输入和配置相关快照从 JSONL 中读取，暂不为它们建立独立索引表。不同会话允许相同的外部 event_id、call_id 或 command_id，数据库身份始终包含 session_id。不在数据库复制一份独立可修改的完整聊天正文。数据库 schema 使用 Alembic；日志格式与 payload 版本单独校验。未知未来日志类型或 payload_version 明确失败，不跳过后继续执行。

### 12.4 并发、尾部修复与重建

- 每个会话分别持有执行锁和追加锁，短时全局维护锁串行化持久化变更；后台文件操作排空后才能释放相关锁，重复取消也不提前释放。数据库投影使用短事务；单进程 asyncio.Lock 不能代替 OS 锁。
- SQLite 开启 foreign_keys、WAL、busy_timeout 和明确同步策略 [D5]。应用事务只覆盖投影更新，不包含 JSONL fsync、网络、模型、压缩或 shell 等待。
- 追平时校验游标对应的记录边界与 hash。当前扫描完整 JSONL 后仅投影游标之后的记录；增量读取属于后续性能改进。seq 分配以验证过的 JSONL 尾部为准，不使用可能落后的数据库 next_seq。
- 活跃 writer 的未完成尾行只等待，读者不得修剪。重启取得会话锁后，如果仅最后一行未写完整，先保留损坏尾部副本，再截断到最后有效记录边界并同步，随后允许追加。
- 中间记录损坏、完整记录 hash 错误、seq 不连续、未知格式均停止恢复，保留原文件并报告；不能删除坏行继续宣称历史完整。
- 数据库缺失时可以从全部有效 JSONL 与 artifact 元数据重建；损坏时使用显式 CLI rebuild，在打开原数据库前执行。所有客户端有 OS lease，维护入口在全局门锁下探测 lease 与执行锁，存在活动持有者便拒绝。先按字节保留原 DB/WAL/SHM，再构建独立 staging 数据库，核对记录数、游标与明确的本地 artifact 引用。外部 MCP JSON 中同名的 artifact_id 不被当作本地引用。
- 已验证 staging 的 checksum 与 READY 状态先持久化，随后隔离旧 sidecar 和主库，再安装新主库。切换中途进程退出，普通 open 可按 manifest 完成已验证切换；未完成验证时明确要求重新执行 rebuild，不能创建空投影掩盖故障。现存会话目录缺少 JSONL、坏行、半行或本地引用损坏均使离线重建失败，保留原事实和备份。
- 会话恢复前必须追平投影。重建只是恢复状态视图，绝不触发工具重放或模型调用。

维护机制参考固定 Codex commit 的 `state/src/runtime/recovery.rs` 对失败数据库及 sidecar 的隔离，以及 `state/src/runtime/threads.rs` 的关联状态删除顺序。本项目采用显式离线 CLI、客户端 lease、校验 staging 和持久化 manifest，以自身恢复协议维护投影。

### 12.5 大输出、备份与清理

ArtifactStore 先写临时文件、刷新与同步、原子改名，再允许 JSONL 记录引用；最后才更新 SQLite 索引。崩溃最多留下未引用文件，不能把还没完成的内容作为持久化引用。读取校验 hash 和大小，缺失内容显示不可用。

备份覆盖 JSONL、被引用的 artifacts 和配置；SQLite 可用一致性备份 API 加快恢复，也可重建。首版备份取得维护锁、暂停新动作并排空 writer，记录每会话最后有效 seq 和 offset，再复制该边界内日志与引用内容。不直接复制活跃 WAL 数据库主文件，不把只有 SQLite 的备份称为完整会话备份。

GC 必须先追平所有投影；存在损坏、落后或无法读取的会话时停止删除。引用判定最终来自保留的 JSONL，不仅看可能过时的数据库行。首版不独立淘汰 JSONL 仍引用的 artifact，避免历史回放失效。完整会话默认保留到用户删除，诊断日志可独立按容量和时间轮转。

用户显式执行 CLI delete 时，离线维护先写持久化 tombstone，再把完整会话目录移到 trash/<operation_id>/session，最后删除关联投影。当前保留隔离目录，不做不可恢复的自动物理清理。启动按 tombstone 完成中断的目录隔离与投影清理；即使恢复旧 SQLite 或把旧会话目录放回原位置，也不会复活已删除会话。需要找回时复制隔离目录到独立 AGENT_HOME，再重建和核验。删除不影响项目工作区或账户凭据。

JSONL 持久化失败属于正确性故障，禁止新副作用。SQLite 投影失败也暂停新动作，但 JSONL 已提交事实保留并可追平。诊断日志导出失败不改变已提交结果；会话 JSONL 与诊断日志虽然都可使用 JSONL 格式，恢复职责完全不同。

## 13 中断和断点恢复

### 13.1 恢复入口

/resume 或 CLI resume 获取会话执行锁，校验 JSONL 尾部与 SQLite 投影游标并先完成追平，再读取最新已提交检查点与后续事件，检查配置、模型协议、工作区和 artifact 完整性，进入可恢复状态。所有恢复操作追加 JSONL 事件并更新投影，不修改过去的执行事实。

默认恢复到待用户继续的状态，展示中断点与待核对动作；不会因为打开会话就自动执行上一次 shell。显式 continue 后才开始新 Run。原进程仍活跃时只能只读查看，不能强抢执行锁。

prepare_continuation 是 TUI /continue 与 CLI continue SESSION_ID 共用的入口：在会话锁内完成恢复，返回未解决的 call_id 供界面提示；未知副作用允许消息与只读操作继续，在 ToolService 派发新副作用时阻止。有未消费输入则返回原队列；没有队列但有未完成任务时创建新的 command_id，并以 continuation_of 关联原 Run。不会重新提交已消费的 command_id，也不会自动重放已完成工具。重复准备及重启后都复用已持久化的续作输入。已完成或空会话明确返回 NO_TASK，避免界面静默无动作。

### 13.2 故障矩阵

| 中断位置 | 恢复行为 |
| --- | --- |
| 用户输入已提交，尚未调用模型 | 队列保留原 command_id；继续时消费一次 |
| 模型请求失败，尚未取得完整响应 | 该 attempt 标记失败；可从最后完整输入发起新 attempt，不能保证模型服务未计费 |
| 流式文本显示一半 | 可保留为未完成展示草稿，不作为完整 assistant item 回传 |
| 工具参数流一半 | 不执行；丢弃未提交调用草稿，保留失败记录 |
| 完整模型响应已提交，工具仍为 READY | 继续时重新检查权限与前置条件后执行，不重新生成该模型响应 |
| READ 工具已发出，结果未提交 | 标记中断；显式继续后可新建 attempt 重查，日期与数据可能变化 |
| patch 已发出，结果未提交 | 比较当前文件与预期前/后 hash；完全匹配后态可确认文件结果，匹配前态可重新评估；其他状态冲突 |
| 多文件 patch 部分完成 | 逐文件核对和显示部分状态，不把整个 patch 当成功，也不覆盖已变化文件 |
| shell 已发出，结果未提交 | UNKNOWN；不得重跑，不以 PID 存在判定原命令成功；核对进程身份、输出和工作区状态 |
| 远程写工具超时或连接丢失 | UNKNOWN；若协议提供查询/幂等键按权威状态核实，否则交还用户决定 |
| 工具结果已提交，下一轮模型未开始 | 直接回放已提交结果，不重新执行工具 |
| 待审批或待用户输入 | 展示原请求；重新验证动作 hash、目标和配置版本，过期请求重新生成 |
| 压缩进行中 | 以 JSONL 中完整有效的 CompactionCommitted 为准；存在则追平 SQLite 后使用新 checkpoint，否则继续旧 epoch |
| MCP 连接断开 | 重建连接并重新确认目录；重连不自动重发此前调用 |
| Ctrl+C / Esc | 等待有界清理，已确认本地终止按实际结果持久化；只有结果确实未知时保留 UNKNOWN，已发生修改保留 |
| JSONL 已提交、SQLite 未更新 | 从投影游标幂等追平，仅重建状态，不重执行工具 |
| JSONL 追加完成但确认丢失 | 核对 event_id、seq 与 hash，承认已存在记录，不重复追加或重新受理输入 |
| JSONL 尾部只有半行 | 无活跃 writer 且已取得锁后，备份损坏尾部并修复边界；未完成动作按工具状态处理 |
| JSONL 中间损坏或 seq 不连续 | 停止恢复，保留原文，报告损坏位置；不能跳过后继续执行 |
| SQLite 缺失或损坏 | 缺失时从日志重建；损坏先备份隔离，再构建并验证新投影，不删除会话事实 |
| artifact 引用缺失 | 显示原文不可恢复，不能从 SQLite 索引伪造内容；必要时由用户恢复备份 |

不存在通用的“外部动作 exactly once”保证。JSONL 记录执行意图与已知结果，数据库事务保证其投影与游标一起更新；二者与文件修改、shell 或远端服务均没有共同事务。恢复日志和数据库不等于恢复外部世界。

运行恢复与文件回滚分别提供。恢复会话不恢复 Git 工作树。首版保留修改前后快照与 diff，但不自动 git reset、clean 或 stash；未来显式撤销也必须检查当前文件是否已被用户修改。

## 14 权限与信任边界

RuntimeConfig 和 ToolContext 使用 ApprovalMode 枚举：ask（默认）保留已有 allow_write、allow_commands 授权，其他副作用请求批准；never 对写入、进程和 MCP 远端调用跳过用户审批；read_only 在派发前拒绝所有非 READ 工具，即使 allow flags 已开启。模式不绕过路径范围、指令读取、参数校验、文件冲突和持久化屏障。内部取消和退出的进程清理不经工具审批。

PolicyEngine 使用工具类型、目标路径、工作区、命令和个人授权范围判断 allow / ask / deny。模型、Skill 和 MCP server 声明不能直接批准自己的动作。

默认工作区读取可用；用户信任工作区后可授予限定范围的编辑权限。shell、跨工作区写入和远程副作用按显式策略处理，避免用命令关键词黑名单冒充安全沙箱。Windows junction、符号链接与规范化路径在实际文件操作前核对；本机任意代码执行仍受当前 OS 用户权限约束。

权限请求绑定 call_id + 参数 hash + workspace + policy_revision。范围授权与单次授权分别存储；参数变化或恢复后目标改变需要重新判定。恢复不会把历史的一次允许扩张成永久权限。

凭据通过环境变量引用或 OS 凭据存储获取；数据库与日志不存令牌。会话文本、代码、Skill 和工具输出可能包含私人内容，保留在用户数据目录并提供导出/删除入口。诊断导出默认不含正文，完整历史导出是独立操作。

## 15 日志与可观测性

区分三条通道：

1. **会话 JSONL 与 SQLite 投影**：前者用于事实回放，后者用于查询与状态读取；持久化或追平失败必须影响执行。
2. **模型上下文记录**：准确保存实际发送/接收内容及版本，受本地访问权限保护。
3. **诊断日志与 Trace**：用于定位性能和故障，可轮转、采样；导出失败不能重执行工具。

关联 session_id、run_id、step_id、call_id、attempt_id、trace_id。日志为结构化 JSON，记录配置 hash、状态转换、工具名、参数摘要、错误码、耗时和结果大小；默认不记录密钥、完整 prompt、源代码和工具正文。

至少提供这些可观察项：

- 模型：请求次数、重试、TTFT、总耗时、输入/输出/缓存 token、缺失 usage。
- 工具：排队、审批等待、执行耗时、截断、超时、UNKNOWN 次数。
- MCP：连接状态、实际协议版本、目录更新、调用和认证失败。
- 上下文：估计与实际 token、压缩原因、前后预算、失败和 context_epoch。
- 缓存：prefix_revision、变化原因、服务端缓存读写用量。
- 持久化：JSONL 队列与同步时延、日志 seq、SQLite applied_seq、投影落后量、busy、失败、尾部修复与重建结果。
- TUI：事件积压、合并 delta、显示落后于提交的游标。

/status 展示当前状态与用量；工具卡片展示相关错误和可继续动作。遥测网络导出默认关闭，使用有界缓冲；日志按大小和保留期轮转。异常携带明确错误码与原始 cause，用户提示由展示层组织。

## 16 预算与故障处理默认值

| 项目 | 初始建议 | 原则 |
| --- | --- | --- |
| 单 Run 模型 step | 40 | 达到后 PARTIAL，允许用户继续 |
| 工具调用数 | 120 | 包括失败调用；MCP 多轮协议重试单独计量 |
| 并行读取 | 4 | 写入与 shell 串行 |
| Run 时限 | 30 分钟活动执行时间 | 用户等待暂停活动计时，另有明确交互过期时间 |
| 网络重试 | 当前无隐式请求重放 | 流中断或结果未知保留现场；用户明确继续；上下文溢出仅允许一次压缩补救 |
| 单工具模型可见输出 | 默认约 8K token，上限受 H 限制 | 完整输出落盘，可按范围取回 |
| 模型输入溢出补救 | 1 次 | 必须先有可用压缩结果，不能无限删除消息 |
| 压缩软阈值/目标 | 95% 完整窗口 / 55% 可用输入预算 | 模型参数变化时重新计算 |

这些值在真实任务验证后调整，不承诺 24 小时内完成全部工程能力。应用层与 SDK 的自动重试不能叠加放大；每个失败 attempt 有可追踪记录。工具错误可回填模型，配置错误和内部不变量错误不转为空结果。

## 17 验证方法与实现阶段

### 17.1 验收场景

| 场景 | 验收标准 |
| --- | --- |
| 基础 coding | 真实小仓库中检索、读文件、修改、运行测试、展示 diff；结果含实际退出码 |
| TUI | 流式输出时仍可输入、滚动、取消；长输出不无限占内存；中文、粘贴和窄屏在 Windows 实测 |
| 协议边界 | Responses reasoning 输入省略 status 并保留摘要与加密内容；空或未知外部错误码合法，字段类型错误明确失败，安全诊断不回显载荷 |
| 运行中输入 | steer 持久化确认、完整批次之间消费、最终关闭边界与并发输入、失败后未消费消息保留 |
| 工具配对 | 并发乱序、失败和取消后，每个已提交调用都有准确对应结果，不串入下一轮 |
| 文件冲突 | 读取后外部修改文件，patch 必须报冲突；不覆盖用户修改 |
| 个人 Skill | 显式根目录加载、同名区分、按需读取、引用路径约束、版本更新可见 |
| MCP | 真实 stdio 子进程与本地 HTTP 测试 server；协议协商、分页、schema 更新、只读超时 FAILED、远端副作用 UNKNOWN、断线后新请求重连且不重放旧调用 |
| 检索 | 忽略规则、无匹配/失败区分、Unicode、特殊字符查询、截断与超时语义 |
| 缓存 | 相同输入快照前缀稳定；明确变更才失效；真实 API usage 观测而非伪造命中 |
| 压缩 | 不同窗口的 95% 触发与 25% 目标、完整窗口摘要输入、超限缩批、跨批历史完整性与工具配对；后续批次失败或取消保留原窗口，恢复不重复摘要 |
| 持久化 | 真实 JSONL 与 SQLite；日志同步、半行尾部、ACK 丢失、事务回滚、磁盘错误、锁冲突、重复 command_id、迁移与完整备份 |
| 双存储恢复 | JSONL 提交后数据库失败、投影游标不前移、重复回放不重复动作、SQLite 丢失可重建、中间日志损坏不跳过 |
| 保留与删除 | 投影落后时 GC 不删正文；删除中断后不复活会话；备份含有效日志边界和被引用 artifacts |
| 恢复 | 在模型完成、工具意图提交、文件替换、结果提交、压缩切换各点杀进程再启动，符合恢复矩阵 |
| 进程 | timeout/取消停止受控进程树；无法确认的结果标 UNKNOWN；不盲目重跑 |
| 可观测性 | 单次 Run 可关联全部调用；日志不泄露凭据；日志导出失败不重复副作用 |

模型决策使用可控的脚本化 provider 验证状态机；另保留显式启用、会消耗 API 的端到端验证。MCP 测试 server 仅为 client 协议测试资产，不计作矿业业务 server 交付。JSONL 追加、数据库、进程与文件恢复必须用真实系统行为验证，不能只 mock。

### 17.2 落地顺序

**P0 工程与协议骨架**：依赖锁定、Pydantic 契约、状态机、配置和凭据引用、JSONL writer 与 durability barrier、SQLite 初始迁移和幂等 Projector、会话锁、Responses adapter。验收纯模型会话恢复及删除 SQLite 后的完整投影重建。

**P1 编码闭环与 TUI**：rg、文件、patch、受控进程、权限请求、文本流与工具事件、消息队列、取消。验收真实代码修复和中断后查看状态。

**P2 个人扩展与长上下文**：个人/目录指令、Skill 目录与正文、MCP 真实连接和延迟发现、稳定前缀、缓存指标、压缩与大输出回读。验收长任务及自有 Skill/MCP 使用。

**P3 故障恢复与日常可用性**：完整故障注入矩阵、断网、UNKNOWN 处置、多进程锁、备份恢复、观测界面、输入体验和性能检查。

P0–P3 都属于用户要求的第一阶段 client。恢复与事件设计从 P0 建立，不能等 P3 才补持久化。矿业三个 server 和日报提示在第一阶段验收后另行实现。

## 18 实施前核验与后续演进

目前已确定 Python、单 Agent、Textual、JSONL 会话事实加 SQLite 状态投影、MCP 官方 SDK、rg 检索、独立上下文窗口和有界恢复。以下事项有明确核验时机，不妨碍按当前设计开始工程：

- 实际账户的模型可用性：登录后查询目录并以实际推理结果为准；默认请求 gpt-6.1-sol，不静默替换。没有凭据仍可完成本地状态机与协议测试。
- Textual 的中文输入、换行组合键和 Windows 终端表现：P1 实测；不根据库宣传承诺 IME 行为。
- Windows 进程树清理与文件替换：P1 先做可复现实验，失败则修执行器，不把限制隐藏成“取消成功”。
- JSONL 同步语义、SQLite 实际运行库版本、WAL 完整性与跨载体备份：依赖锁定时核查官方修复状态，P0/P3 用真实文件验证，不以 flush 或内存 mock 代替持久化证明。
- 用户已有 Skill/MCP 路径与认证方式：通过配置接入，不扫描或导入整个 Codex 私有目录。远程 OAuth 需要时使用 SDK 的标准流程和独立凭据存储。

后续可以增加模型供应商、显式任务完成策略、更多 UI 与远程入口。通用 Runtime 不引入新闻、矿产或日报字段；Markdown 日报只是客户端配置的一种输出任务。

## 19 OpenAI 订阅登录

### 19.1 参考与接口边界

参考 D:/code/gongkao-hub/src/gongkao/model_connections/ 的 oauth.py、local_helper.py、service.py、adapter.py；读取时仓库 HEAD 为 29f7a1d9ab93809ae3a2222d7141f7badbe078a8。参考的是本地工作树，不能据此宣称它没有未提交修改。

该实现采用官方 Sign in with ChatGPT 开源客户端流程，包含动态注册、PKCE、本地回调、OIDC 校验、刷新意图与 Responses 流解析。本项目移植这些机制，不引入其 LangChain、PostgreSQL、多用户平台或远端凭据传输。官方依据见 [A1][A2][A3]。

认证模式为 chatgpt 或 api_key，用户显式选择；订阅额度耗尽、账户不符合条件或授权失败时不能自动切到 API key 计费。chatgpt 模式只向核验的 OpenAI 身份服务及 https://api.openai.com/v1 发送对应凭据，不把令牌发送到用户自定义 endpoint。

### 19.2 登录与身份

提供 CLI auth login/status/logout/models，以及 TUI 的 /login、/logout 与账户状态。登录只有用户调用登录入口时才开始；测试默认使用本地模拟身份服务，不自动读取已有 Codex 或 gongkao 凭据。

AuthService 在打开系统浏览器之前启动 127.0.0.1 的临时端口监听，回调路径固定 /auth/callback；授权和换码使用同一个 URI。生成独立 state、nonce 与 PKCE S256 verifier，设置 10 分钟超时，一次性消费回调。拒绝重复参数、state 不匹配、非预期路径、过大请求和过期回调。

首次使用 dynamic_agent_client 申请本应用的注册；保存回调颁发的 client_id，后续对该账户复用。稳定 host_id 独立存放。使用真实应用名称 Agent Client，不冒充 Codex。校验 ID token 的 JWKS 签名、issuer、audience、expiry、nonce 和已选账户 subject；需要订阅调用 scope，不能以登录成功替代推理权限检查。

新登录在完成身份和 scope 校验前不替换已选账户。首版管理一个活动账户及其注册身份；重新授权需匹配原 subject，换账户必须走显式新登录。仅保留必要的账户显示字段，不把 email 当作身份主键。

### 19.3 凭据持久化与刷新

凭据存储独立于会话 JSONL 和 state.sqlite；这是认证事实的独立权威来源，不能从聊天日志重建。Windows 使用当前用户 DPAPI 保护凭据文件，POSIX 使用独立用户目录和 0600 权限的原子文件。密钥、access_token、refresh_token、id_token、code、verifier、完整授权 URL 不进入日志、Trace、异常展示或数据库投影。

凭据包包括注册身份、host_id、subject、scope、expiry、earliest_refresh_at、token_generation 和认证状态。临时文件写入、同步后原子替换；创建与更新均受账户级 OS 锁保护，不能让两个进程同时刷新同一 rotating refresh token。

刷新在网络请求前持久化 REFRESHING 与 generation，释放文件 I/O 临界操作后请求官方 endpoint，成功后一次性替换整包令牌与状态。账户 OS 锁覆盖刷新所有权，但不能持有 SQLite 事务等待网络。遵守 earliest_refresh_at；成功响应经身份与 scope 校验后原子替换旋转令牌；身份或 scope 改变、明确的失效错误、成功旋转后的校验或持久化失败，以及进程在 REFRESHING 状态崩溃时，进入 REAUTH_REQUIRED，不重用旧 refresh token。旋转前发生传输失败或服务暂时拒绝时保留凭据并报告安全错误，当前刷新请求不自动重放；后续独立请求按认证状态判断是否允许续期。

logout 先禁用本地使用，再尝试撤销当前会话。撤销失败清楚显示远端结果未知，不宣称远端已退出；本地令牌清理和保留公开 client_id/host_id 的策略明确执行。普通会话恢复只读取非敏感账户引用，认证无效时暂停模型请求，保留编码任务进度。

续期使用凭据中保留的 issued client_id，以 form-encoded refresh_token grant 请求官方 token endpoint，不额外发送 scope。认证诊断仅记录安全 HTTP 状态和受控错误分类，不输出凭据或服务端自由文本。明确失效时结束该凭据会话，结果未知不能被当作已成功续期。

### 19.4 订阅模型调用

ResponsesGateway 从 AuthService 获取 bearer token，使用 store=false、stream=true，保留完整原生 output items 与 tool call ID。namespace 工具编码和流式 response.output_item.done / response.completed 的组装参考 adapter.py，响应完成以前不执行工具。最终 envelope 缺少 output 时仅使用已完整收齐并校验的 item.done 事件，不能把部分 delta 当作完成。

将认证过期、额度耗尽、模型无权访问、服务临时不可用与流中断分别映射到类型化错误。流已产生输出后断开不透明重放；模型请求的结果未知与外部工具的结果未知分开记录。授权刷新成功不意味着模型请求可无限重试。

订阅路径的缓存和原生压缩只发送已验证支持的参数；不支持的能力显示限制，不更换认证模式绕过限制。summary 压缩可通过同一已授权模型的普通无工具请求完成。

历史仍存完整 NativeReasoning；HTTP 请求边界使用独立的 ResponsesReasoningInput 契约，保留 id、summary、非空 content 与 encrypted_content，不发送 status，不改写会话事实或删除推理上下文。参考固定 Codex commit `19b7bffd7bd5c325a45b91111ce64c85610b90ba` 的 `codex-rs/protocol/src/models.rs:1051`，其 Reasoning 序列化同样不包含 status。其它消息、函数调用与结果保持原顺序和配对。

外部 error.code 属于开放协议字符串，可为 null；message、param、type 为具名可空字段，在协议边界校验形状，内部已知分类仍使用枚举。界面保留 HTTP 状态、已知错误码和安全参数路径，不直接输出可能回显凭据或请求内容的供应商自由文本。非 JSON 或字段类型错误仍明确报告协议错误，不把合法的未知错误码当成协议损坏。

### 19.5 验收与实施范围

必须覆盖：PKCE/state/nonce、issued client_id、重复回调、过期与拒绝、JWKS 验证、scope 不足、账户混淆、凭据文件保护、刷新并发与崩溃、注销结果未知、SSE 分帧和未完成响应不执行工具。

真实账户登录与实际模型可用性只能由用户完成授权后验证；不读取其他应用的令牌、不代用户输入密码。缺少真实登录不影响工程、协议模拟和本地编码恢复测试，但交付时须明确未验证的账户级结果。

[A1]: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
[A2]: https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
[A3]: https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference

## 20 源码与官方资料

以下 Codex 链接全部固定到设计参考 commit，方便后续复核；不以不断变化的 main 作为实现证据。

[C1]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/src/client.rs
[C2]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/src/session/context_window.rs
[C3]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/src/compact.rs
[C4]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/src/session/rollout_reconstruction.rs
[C5]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/src/context_manager/normalize.rs
[C6]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/rollout/src/recorder.rs
[C7]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/state/src/runtime.rs
[C8]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/rollout/src/writer_lock.rs
[C9]: https://github.com/openai/codex/tree/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/ext/skills/src
[C10]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/src/agents_md.rs
[C11]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/src/mcp_tool_exposure.rs
[C12]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/file-search/src/lib.rs
[C13]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/otel/src/events/session_telemetry.rs
[C14]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/tui/src/app_event.rs
[C15]: https://github.com/openai/codex/blob/19b7bffd7bd5c325a45b91111ce64c85610b90ba/codex-rs/core/gpt-5.2-codex_prompt.md
[M1]: https://github.com/modelcontextprotocol/python-sdk/blob/2118f14f8a19bc158d8a1cf90af58d85d187f849/docs/protocol-versions.md
[D1]: https://developers.openai.com/api/docs/guides/prompt-caching
[D2]: https://developers.openai.com/api/docs/guides/compaction
[D3]: https://textual.textualize.io/guide/workers/
[D4]: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks
[D5]: https://www.sqlite.org/wal.html
