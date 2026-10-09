# Agent Client

本机已安装版本的使用步骤见 [本地操作指南](docs/USER_GUIDE.md)：直接运行 `agent-client`，无需进入源码目录或激活虚拟环境。下文保留源码开发、协议边界与验证说明。

一个在本地项目目录工作的异步 Python 编码 Agent。终端界面和非交互 CLI 共用执行内核，支持本地文件工具、受控命令、个人 Skills、MCP、上下文摘要与中断恢复。设计契约和实现取舍统一维护在 [DESIGN.md](docs/DESIGN.md)。

默认通过独立的 **Sign in with ChatGPT** 授权使用订阅模型，也可显式配置 `openai_chat_completions` 与 API key 接入 DeepSeek 或兼容服务。应用名称为 Agent Client，不读取其他应用的账户凭据，不因额度耗尽自动切换认证方式。0.1.4 已通过真实 DeepSeek `deepseek-flash` 两轮模型与 `read_file` 工具交互，最终返回 `444`，推理内容和 usage 正常；0.1.12 已通过真实 ChatGPT 与 DeepSeek 摘要压缩和后续请求，以及 ChatGPT 续期接口验证。本机当前安装 0.1.16，正文与 reasoning 支持自适应分帧呈现，积压自动追赶；保留工具详情正文点击收起及拖动文字选择。

## 安装

项目要求 Python 3.12 或更新版本。当前验证环境为 Windows、CPython 3.12.7 和 3.14.3。Windows 可用 WinGet 安装 uv 和 ripgrep；安装后重新打开终端，确认 `uv` 和 `rg` 已在 PATH 中。[uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)、[ripgrep 安装说明](https://github.com/BurntSushi/ripgrep#installation)。

```powershell
winget install --id astral-sh.uv -e
winget install --id BurntSushi.ripgrep.MSVC -e
uv --version
rg --version
uv python install 3.14
uv sync --locked --python 3.14
uv run --locked agent-client --help
```

在仓库根目录运行命令。`uv sync --locked` 使用仓库中的 `uv.lock`；锁文件和依赖声明不一致时会报错，不会自行改锁文件。[uv 锁定与同步说明](https://docs.astral.sh/uv/concepts/projects/sync/)。

用户数据默认放在 `~/.agent-client`。可通过环境变量 `AGENT_HOME` 或每条命令的 `--home` 指定独立目录。配置默认读取该目录的 `config.toml`；缺少默认配置时使用应用默认值。显式传入不存在或内容非法的 `--config` 会直接失败。

日常直接运行 `agent-client`。切换 ChatGPT 订阅、DeepSeek 或其他端点时，修改默认 `config.toml` 的 `[model]` 段，再重启；不需要每次传入 `--config`。认证方式标识保存在该段，实际订阅凭据独立加密保存。完整切换示例见 [操作指南](docs/USER_GUIDE.md#5-个人配置与模型)。

## 授权与模型

先查看本应用的账户状态，再执行独立登录。登录会启动临时的 `127.0.0.1` 回调服务并打开系统浏览器；只有用户明确调用登录入口时才开始。

```powershell
uv run --locked agent-client auth status
uv run --locked agent-client auth login
uv run --locked agent-client auth models
uv run --locked agent-client auth status
```

`auth models` 查询当前账户公开可用的模型。默认模型为 `gpt-6.1-sol`，具体可用性由实际授权账户决定；可在配置的 `model.model` 或 TUI 的 `/model MODEL` 中选择。TUI 中修改模型只对当前进程生效。

常规重新授权必须匹配原账户的 subject；明确换账户使用 `auth login --new-account` 或 `/login new`。`auth logout` 先停止本地令牌使用，再尝试撤销远端会话；输出的 `revocation` 为 `unknown` 时，不能据此判断远端已退出。

Windows 凭据文件通过当前用户 DPAPI 保护，POSIX 使用独立目录和 0600 原子文件。刷新前先持久化意图；刷新中断或结果无法确认时要求重新授权，不重用旧 refresh token 重试。凭据不进入会话 JSONL、SQLite 投影或诊断日志。

API key 是用户显式选择的另一种认证模式。将配置的 `model.auth_mode` 改为 `api_key`，并在主机环境中提供 `model.api_key_env` 指定的变量。配置中只写变量名称，不写密钥。订阅路径固定使用官方 OpenAI endpoint；不向自定义地址发送订阅令牌。

## TUI 与 CLI

启动时显示 `Agent Client · ShaneGuo`。TUI 顶部保留该标识，命令行启动标识写入 stderr，结构化结果仍从 stdout 输出。

默认本地 `context_window` 为 `256000`，底部显示如 `Context 12.4k / 256k · 4.8% · last input` 的上次完成请求的服务端输入用量。吐字和工具执行期间不估算增量、不刷新数字；尚无服务端计数时显示 unavailable。内部在完整窗口的 95% 触发压缩，压缩后目标为窗口的 25%。该设置只改变本地预算，不增加服务端模型容量。等待、thinking 和工具工作时显示低调活动符号及耗时，空闲或等待审批时停止，减少动画模式使用固定符号。

普通 Esc 取消会等待本地进程终止并收集 stdout、stderr、exit code，再经日志持久化屏障记录已确认的取消或完成结果；不能仅因取消就标为 UNKNOWN。此前发生的写入和命令副作用仍保留，取消不意味着回滚。真正无法确定副作用结果的 crash 或远端调用仍保留 UNKNOWN，不自动重放；新消息与读取可继续，后续副作用动作须先核验未知结果。明确只读的 MCP 查询超时记为失败，下一次独立请求可重新连接。

全局选项 `--home`、`--config`、`--workspace`、`--session` 放在子命令之前。下列工作区路径应替换为自己的项目。

```powershell
uv run --locked agent-client --workspace D:/code/my-project
uv run --locked agent-client --workspace D:/code/my-project run "Inspect the project and explain its structure"
uv run --locked agent-client sessions
uv run --locked agent-client resume SESSION_ID
uv run --locked agent-client continue SESSION_ID
uv run --locked agent-client resume SESSION_ID "Continue after reviewing the saved state"
uv run --locked agent-client diagnostics
```

无子命令进入 TUI。Enter 提交，Shift+Enter 换行，Ctrl+J 可作为换行备用键，仍支持 Ctrl+Enter 提交。输入框从一行开始，随内容增长到八行，之后在框内滚动。界面没有 Send、Cancel 或审批按钮。在输入开头键入 `/` 展开命令候选，随输入模糊筛选；上下键选择、Tab 或 Enter 补全，完整命令按 Enter 执行。Esc 优先关闭候选或权限列表、拒绝当前审批，否则取消当前运行；Ctrl+Q 退出。流式输出期间仍可输入，新输入先持久化再显示已接收，按顺序排队执行。运行失败、部分完成或取消后，后续输入保留在队列中。

用户输入用底色和 `›` 区分，回复以 `•` 开始，用留白保持对话层次，不显示 Task/Agent 编号和外层任务框。接口返回的可读 reasoning 独立显示为浅灰色，同一 reasoning item 优先展示摘要；没有返回时不补造文本，加密内容不显示。正文与 reasoning 通过独立呈现任务分帧显示，积压时自动追赶；正文保留已完成 Markdown 段落，恢复历史和取消时直接追平。工具按调用身份持续更新，默认只显示摘要和红绿 diff；详情折叠，diff 摘要最多十二行，展开预览最多一百二十行并有字符限制。ID、token 用量和原始事件通过 `/status` 查看。审批在输入框上方显示完整命令或路径，可滚动查看；上下键选择，Enter 确认，默认 Deny。

`resume SESSION_ID` 和 `/resume SESSION_ID` 只恢复并显示状态，**不会自行消费队列或启动工具**。检查进度后使用 `/continue` 或 CLI `continue SESSION_ID` 显式续作：先处理已有输入队列；没有队列时，为中断、取消、失败或部分完成的任务建立新 Run，沿用已提交进度。已完成或尚无任务的会话会提示无待续任务；UNKNOWN 必须先核验并 resolve。登录状态或预算限制修好后，也使用这个续作入口。带有 prompt 的 CLI `resume` 是明确的新执行请求，会按顺序处理已有队列。

恢复界面按任务重建最近二十条用户输入对应的回复、工具与状态，并展示待处理队列；更早事实仍保存在完整 JSONL 中。恢复不会执行模型或工具。每段显示和工具预览有大小限制，不应将界面预览当作完整会话导出。

| TUI 命令 | 作用 |
| --- | --- |
| `/new`、`/sessions`、`/resume ID` | 新建、查看和恢复会话 |
| `/continue` | 显式处理输入队列，或为未完成任务建立新 Run 续作 |
| `/login`、`/login new`、`/logout` | 独立账户授权与退出 |
| `/model`、`/model MODEL` | 查看或设置当前进程的模型 |
| `/reasoning`、`/reasoning LEVEL` | 选择或设置当前进程的推理强度 |
| `/permissions`、`/permissions MODE` | 选择或设置当前进程的审批模式 |
| `/skills QUERY`、`/mcp` | 查看 Skills 元数据与 MCP 连接状态 |
| `/compact` | 使用同一已授权模型生成上下文摘要 |
| `/status`、`/config` | 查看账户、工作区、权限、队列、usage 与日志路径 |
| `/resolve CALL_ID failed\|succeeded EVIDENCE` | 记录未知工具结果的人工核验依据 |
| `/rebuild`、`/delete ID` | 提示关闭客户端后使用维护 CLI |

usage 未提供时显示 `unavailable` 或 JSON `null`，不将缺失值当作零。诊断日志位于用户数据目录的 `logs/runtime.jsonl`，轮转保存；它不是恢复事实来源。工具输出和 diff 只显示有限预览，完整结果保存在会话 artifacts 中。

`/permissions` 在输入框上方打开选择列表，上下键选择、Enter 确认、Esc 关闭。模式为 `ask`（默认，保留 `allow_write` / `allow_commands` 授权）、`never`（写入、命令和 MCP 均不请求审批）、`read_only`（拒绝全部非读取工具）。模式不改变工作区路径和文件冲突校验。运行期间不能切换；选择只对当前进程生效，不写回配置。持久配置使用 `runtime.approval_mode`，启动覆盖使用 `--approval-mode`，例如 `agent-client --approval-mode never run "Fix the failing test"`。

## 配置、Skills 与 MCP

[config.example.toml](config.example.toml) 只包含当前支持的字段，默认不启用 MCP 或额外权限。可通过 `--config config.example.toml` 检查并使用示例，或将它作为个人 `config.toml` 的起点。`context_window` 是本地预算设置，需要按所选模型的实际能力配置；当前没有完成真实账户容量验证。

[config.deepseek.example.toml](config.deepseek.example.toml) 配置 API key 与 `deepseek-flash`；[config.openai-compatible.example.toml](config.openai-compatible.example.toml) 配置通用 `openai_chat_completions` 服务。key 由 `model.api_key_env` 引用环境变量，endpoint 使用 `model.base_url`。API key 模式的 `auth status` 只检查 key 是否存在，`auth login` 无浏览器地请求模型目录验证 key，`auth models` 返回目录；这些命令不修改 ChatGPT 账户。会话绑定 provider、endpoint、auth_mode，改变后必须新建会话。

`/reasoning` 打开上下键与 Enter 选择菜单，`/reasoning high` 直接选择，启动参数为 `--reasoning-effort high`。选择范围由 `model.reasoning_levels` 决定，DeepSeek 示例为 `none/high/max`；选择仅影响当前进程，不写配置，运行期间不能切换。通用 Chat Completions 默认不发送 thinking/effort 附加字段；显式选择强度会启用 effort 参数，需匹配服务能力。

通用 chat 的 `chat_reasoning = "default"` 不能与 `reasoning_effort = "none"` 组合，直接配置也会立即报错。旧会话缺少 endpoint 或认证绑定时，只允许原默认官方 Responses + ChatGPT 配置续作或压缩；切换到 DeepSeek 或其他兼容服务须新建会话。

个人 Skills 是带有 `SKILL.md` 的目录。配置根目录后，应用加载名称和说明形成目录，按需读取正文与目录内资源；`/skills` 可检查发现结果。

```toml
[skills]
roots = ["~/.agents/skills"]
catalog_token_budget = 2000
```

MCP 支持 stdio 与 Streamable HTTP。下面是需要自行安装相应服务后再添加的配置片段；它们没有默认启用。

```toml
[mcp.servers.local]
transport = "stdio"
command = "my-mcp-server"
args = []
required = false
timeout_seconds = 60
env = { SERVICE_TOKEN = "MY_MCP_SERVICE_TOKEN" }

[mcp.servers.remote]
transport = "streamable_http"
url = "http://127.0.0.1:8000/mcp"
required = false
timeout_seconds = 60
token_env = "MY_MCP_HTTP_TOKEN"
```

stdio 的 `env` 中，左边是子进程变量名称，右边是**主机已有环境变量的名称**，不是实际值。上例把主机 `MY_MCP_SERVICE_TOKEN` 的值传给子进程 `SERVICE_TOKEN`。`token_env` 同样只引用主机变量。缺少变量或连接失败会显示连接错误；`required=true` 的服务失败会阻止启动。

MCP 变量优先读取进程环境；Windows 进程缺少指定变量时，异步读取已保存的 User、Machine 环境，不枚举或导入全部环境。显式空值立即失败。启动界面和模型工具发现结果均显示服务连接状态，区分连接失败与搜索未命中。工具检索使用短关键词，空查询列出全部目录。

MCP 工具发现和调用分开，schema 变化后需要重新发现。远端内容和工具输出作为不可信材料处理，不赋予新的权限。

当前没有宣告支持 MCP OAuth、elicitation、sampling 或 roots 回调。需要认证的现有 HTTP 服务使用显式环境变量中的 bearer token；不能把 SDK 具备的其他能力当作本应用已经接入的能力。

## 权限与未知结果

默认写文件和执行命令需要逐次批准，批准弹窗默认选中拒绝。非交互 stdin 无法回答批准请求时拒绝执行；需要自动写入或运行命令时，用户可显式选择 `--allow-write`、`--allow-commands`，或设置对应个人配置。MCP 工具调用仍需要批准。

这些权限是应用执行边界，**不是操作系统沙箱**。获准的 shell 命令具有当前用户的系统权限。工作区文件工具限制路径范围，并用内容 hash 检查写入冲突；shell 权限不能因此视为受到同样的文件限制。

工具已派发后进程退出、取消或连接中断，可能留下 `UNKNOWN`。此时会话阻止继续模型执行，避免重复写文件、重复命令或重复远端调用。先检查文件、进程或远端实际状态，再记录核验结果：

```powershell
uv run --locked agent-client resolve SESSION_ID CALL_ID failed "Inspected the workspace; the operation did not complete"
uv run --locked agent-client resolve SESSION_ID CALL_ID succeeded "Confirmed the resulting file and external receipt"
```

`resolve` 只记录人工结论与依据，不重新执行工具；依据不可省略。后台命令的 process handle 属于当前应用进程，重启后不能据它认定原命令结果。模型流未完整结束时也不会执行部分收到的工具调用。

## 备份、恢复与维护

先停止运行，再把备份写到用户数据目录之外的一个不存在的目标目录：

```powershell
uv run --locked agent-client backup D:/agent-backups/snapshot-001
```

备份包含会话 JSONL、artifacts、个人配置和 manifest；不包含认证凭据，也不依赖复制 SQLite。恢复时，把备份中的 `sessions` 目录和可选配置复制到一个独立用户数据目录，使用该目录重新登录，再根据 JSONL 重建投影。备份中的工作区路径仍指向原项目；需要确保这些目录及相关文件存在，不能把备份视为项目代码备份。

```powershell
uv run --locked agent-client --home D:/agent-restored rebuild
uv run --locked agent-client --home D:/agent-restored sessions
uv run --locked agent-client --home D:/agent-restored auth login
uv run --locked agent-client --home D:/agent-restored resume SESSION_ID
```

`rebuild` 在打开 SQLite 前执行，适用于投影数据库丢失或损坏。它先保存原投影文件并校验会话事实，再建立新投影；输出维护记录和保存位置。JSONL 损坏时明确失败，不能靠诊断日志补造事实。重建不会调用模型或重放工具。

输出的 `backup_directory` 为 `AGENT_HOME/maintenance/OPERATION_ID/previous`，保存旧 SQLite 及存在的 WAL、SHM 原始文件；同级 `manifest.json` 保存维护阶段和校验信息。这里是维护前的投影副本，不能替代会话 JSONL 备份。

删除只由用户显式命令触发：

```powershell
uv run --locked agent-client delete SESSION_ID
```

维护要求相关客户端和运行停止；检测到活动会话或客户端时拒绝。删除把会话移到可恢复位置并同步投影，输出 `trash_directory`。原用户数据目录会保留删除标记；直接把会话放回原位置，仍会按删除事实重新隔离。

需要找回时，将 `trash_directory/session` 复制到**独立恢复数据目录**的 `sessions/SESSION_ID`，在该独立目录执行 `rebuild`，重新登录后再查看和恢复。不要复制原目录的删除标记或认证文件，也不要覆盖同名现存会话。账户凭据与项目工作区不会因删除会话而删除。

## 验证范围

已通过本地真实 JSONL/SQLite、模拟身份服务和模拟模型传输验证：授权校验与凭据保护、刷新中断、SSE 分帧、工具与消息配对、队列恢复、取消、上下文摘要、UNKNOWN 阻止重放，以及 Textual Pilot 的日常交互。没有使用这些测试替代真实账户验证。

本次未重新进行 ChatGPT 订阅网络调用验收；订阅路径的缓存命中或原生压缩也未宣称为可用。首版摘要压缩使用普通无工具模型请求；订阅缓存 key 等未确认参数不发送。POSIX 上的运行、凭据权限和进程取消需要单独环境验收。

Responses 的 API key 模式发送 `prompt_cache_key` 与 `max_output_tokens`；Chat Completions 使用 `max_tokens`，不发送 Responses 专有参数。订阅模式保持稳定请求前缀并观察服务返回的 usage，`max_output_tokens` 配置仍参与本地上下文预算，不能视为订阅服务已经执行该输出上限。原生压缩未实现。

Pilot 验证覆盖模拟模型下的输入、排队、取消、窄屏和 Unicode 内容显示。真实终端的中文输入法、粘贴行为和账号授权仍需用户环境验证，不能由 Pilot 成功推断。

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$agentCodexDirectory = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$agentVerificationRoot = Join-Path $agentCodexDirectory ("outputs/agent-client-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Force -Path $agentVerificationRoot | Out-Null
uv run --locked python -m pytest --basetemp "$agentVerificationRoot/pytest" -o "cache_dir=$agentVerificationRoot/pytest-cache"
uv run --locked python -m ruff check --no-cache src tests
```

项目工程约定见 [AGENTS.md](AGENTS.md)。上面的验证命令把临时数据和 pytest 缓存放在仓库外的独立输出目录，关闭 bytecode 与 Ruff 缓存。
