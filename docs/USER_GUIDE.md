# Agent Client 本地操作指南

直接运行 `agent-client` 创建新会话。恢复历史会话时，使用 `resume` 或 `/resume` 指定会话。

启动标识为 `Agent Client · ShaneGuo`，TUI 顶部持续可见；命令行在 stderr 打印启动标识，JSON 等结构化结果仍从 stdout 输出。

需要 Python 3.12+、uv 和 ripgrep。安装方法见 [README](../README.md)。安装后重新打开终端，确认 `agent-client` 与 `rg` 命令可用；日常使用不需要进入源码仓库或激活虚拟环境。

## 1. 第一次使用

先确认命令可用，再登录：

```powershell
agent-client --help
rg --version
agent-client auth status
agent-client auth login
agent-client auth models
```

默认 ChatGPT 配置下，`auth login` 会打开浏览器。使用自己的 ChatGPT 账户完成授权，回到终端等待登录结果。这个客户端独立保存授权，不会复用 Codex 或 Claude Code 的登录状态。看到 `signed_out` 代表尚未登录。API key 配置下的命令行为见下文。

默认模型是 `gpt-6.1-sol`。以 `auth models` 返回的实际目录为准；如果账户无权使用默认模型，修改个人配置中的 `model`，或进入 TUI 后使用 `/model MODEL_NAME`。不会静默换模型或自动切换到 API key 计费。

日常直接运行 `agent-client`，它默认读取 `~/.agent-client/config.toml`。切换端点或认证方式时，修改这份文件的 `[model]` 段，保存后退出并重新运行 `agent-client`。使用 DeepSeek 前，先设置 `DEEPSEEK_API_KEY` 环境变量，配置示例见第 5 节。

```powershell
agent-client auth status
agent-client auth models
agent-client
```

API key 模式下，`api_key_configured` 仅表示环境变量中存在 key。`auth login` 请求模型目录，确认服务是否接受该 key，不打开浏览器；`auth models` 列出服务返回的模型。这些操作不改变已保存的 ChatGPT 账户。配置只引用变量名，不保存 key。

选择一个已经存在的项目目录启动。把下面的路径替换为自己的项目：

```powershell
agent-client --workspace D:/code/my-project
```

在输入区写任务，按 **Enter** 提交，**Shift+Enter** 换行。首次可以要求它先阅读项目结构、说明理解和修改方案，再执行修改。默认写文件和运行命令都需要逐次批准。

## 2. 日常交互

| 操作 | 按键或命令 |
| --- | --- |
| 输入换行 | Shift+Enter；也可用 Ctrl+J |
| 提交或引导当前任务 | Enter；运行中为 steer，仍支持 Ctrl+Enter |
| 下一轮排队 | 运行中按 Tab；命令菜单打开时仍是补全 |
| 查看并搜索命令 | 在输入开头键入 `/`，继续输入即可模糊筛选 |
| 选择和补全命令 | 上下键选择，Tab 或 Enter 补全；完整命令按 Enter 执行 |
| 关闭命令候选 | Esc，保留已输入内容 |
| 取消当前运行 | Esc |
| 查看上次请求的上下文用量 | 底部显示 `Context 输入用量 / 窗口 · 百分比 · last input` |
| 复制文本 | 鼠标拖选输出或输入框文字，按 Ctrl+C；也支持 Ctrl+Shift+C |
| 粘贴到输入框 | Ctrl+V，或在输入框内单击鼠标右键 |
| 选择审批模式 | `/permissions`，上下键选择、Enter 确认 |
| 选择推理强度 | `/reasoning`，上下键选择、Enter 确认；也可 `/reasoning high` |
| 退出客户端 | Ctrl+Q |
| 查看状态、权限、用量和日志路径 | `/status` |
| 查看当前配置 | `/config` |
| 新建会话 | `/new` |
| 查看会话列表 | `/sessions` |
| 查看个人 Skills | `/skills` 或 `/skills QUERY` |
| 查看 MCP 连接 | `/mcp` |
| 手动压缩上下文 | `/compact` |
| 登录或退出账户 | `/login`、`/logout` |

运行中按 Enter 发送补充要求，消息会在当前模型响应及完整工具批次结束后进入同一任务；它不会中止正在执行的命令，立即取消使用 Esc。按 Tab 将输入保留为下一轮任务。取消、失败后，未消费的输入可通过 `/continue` 继续。

例如输入 `/stts` 可匹配 `/status`，按 Tab 补全后按 Enter 执行。输入命令参数或普通多行文本时，候选列表自动关闭。粘贴的多行文本保留在输入框中，不因文本中的换行自动发送。有候选列表时，第一次 Esc 只关闭列表，再按 Esc 才取消运行。

Windows 版本会在运行期间启用带修饰键的终端输入协议，退出时恢复。较旧终端若不能识别 Shift+Enter，可用 Ctrl+J 换行。安装更新后，请退出已打开的客户端，再重新运行 `agent-client`；旧进程不会自动加载新版本。

输入框从一行开始，随文本和自动折行增长到八行，再在框内滚动。界面没有 Send、Cancel 或审批按钮。用户输入有底色和 `›` 标记，回复以 `•` 开始，通过留白区分，不再显示 Task/Agent 编号和外框。模型接口返回的 reasoning 用浅灰色单独呈现；同一条 reasoning 优先显示摘要，不与最终回答混合。未返回可读内容时只显示等待状态，不补造思考过程。正文与浅灰 reasoning 按短片段连续呈现，积压时自动加速追赶；恢复历史直接显示全文，取消时立即显示已收到的内容。工具显示简短命令或路径和当前状态，文件 diff 新增为绿、删除为红，摘要最多十二行；工具详情默认折叠，展开最多一百二十行并有字符限制。默认界面不显示 ID、逐次请求 token 明细和原始事件，诊断信息通过 `/status` 查看。

工具标题左键点击展开或收起；展开后，在具体输出正文内单击左键即可收起，并将该工具的折叠标题带回可见区域。右键和拖动选择文字不会触发收起。折叠标题前没有额外空行，内容展开后紧接标题显示。

审批显示在输入框上方，完整命令或路径可滚动查看；上下键选择、Enter 确认，默认选中 Deny，Esc 拒绝当前审批。审批期间输入框暂时禁用。没有候选、权限列表或审批时，Esc 用于取消运行。获准的 shell 命令拥有当前 Windows 用户的权限；这里没有操作系统沙箱。

底部例如 `Context 12.4k / 256k · 4.8% · last input` 只展示上次完成请求的服务端 `input_tokens`，不累加历史用量或输出 token。吐字、推理和工具执行期间不会估算增量或刷新数字。新会话、响应未提供 usage、压缩后尚无新请求计数时显示 `-- / 256k · unavailable`。因此这行是上次请求的记录，不包含尚未发送的新增内容。

默认上下文窗口为 `256000`，需按所选模型能力配置。达到窗口的 **95%** 时自动压缩，压缩后目标为窗口的 **25%**：256k 对应不超过 64k。也可使用 `/compact` 手动压缩。压缩失败或取消会保留原历史，处理错误后可重试；界面的 last input 数字不会替代发送前的预算判断。

Windows 下复制、Ctrl+V 和输入框右键粘贴使用系统剪贴板，支持中文和多行文字；多行粘贴不会自动提交。Ctrl+C 没有选区时不做操作，也不退出应用；取消任务仍用 Esc，退出仍用 Ctrl+Q。

工作时显示 `· ✧ ✦ ✧` 循环及经过时间，低调地以每秒四次更新；空闲和等待审批时停止，减少动画模式使用固定符号。普通 Esc 取消确认本地进程终止后，可使用 `/continue` 继续。已发生的文件或命令副作用不会回滚。旧版本已有 UNKNOWN 的会话可以继续发消息和读取信息；核验实际结果并 resolve 之前，禁止新的有副作用操作。升级不会自动解除 UNKNOWN，也不会重放原调用。

`/permissions` 在输入框上方打开选择列表，也可直接输入 `/permissions never`。`ask` 是默认模式，按工具请求批准，并保留已有 `allow_write`、`allow_commands` 授权；`never` 跳过写入、命令和 MCP 审批；`read_only` 拒绝所有非读取工具。目录范围、指令读取和文件冲突检查仍有效。运行期间不能切换，选择仅对当前进程生效，不写回配置。

界面只保留有限的历史和工具预览。完整会话在 JSONL 中，完整工具输出保存在 artifacts 中。`unavailable` 或 `null` 表示服务没有提供该项用量，不代表消耗为零。

## 3. 中断后恢复与续作

先找到会话 ID：

```powershell
agent-client sessions
agent-client resume SESSION_ID
```

`resume` 只恢复显示，不自动调用模型或工具。界面重建最近二十条用户输入对应的回复、已持久化 reasoning、工具和完成状态，并展示待处理输入。检查状态后，在 TUI 输入：

```text
/continue
```

也可以直接在 PowerShell 续作：

```powershell
agent-client continue SESSION_ID
```

续作会先处理已有输入队列。如果队列为空，但原任务失败、取消、中断或达到预算，会创建新的 Run，沿用原任务的上下文和已提交进度。已完成或空会话会明确提示无任务可继续。

常见情况：首次因未登录而失败，完成 `auth login` 后执行 `continue`；达到运行预算时，退出客户端、调整个人配置，再执行 `continue`。再次发起续作不会自动重放已完成工具。

若需要补充新要求，可以直接提交新消息，或使用：

```powershell
agent-client resume SESSION_ID "Continue the task and run the targeted tests"
```

## 4. 非交互执行

单次任务也可以直接在终端运行：

```powershell
agent-client --workspace D:/code/my-project run "Inspect the project structure and summarize the main modules"
```

全局选项必须放在子命令前，例如 `--workspace`、`--config`、`--home`。如果需要明确允许该次运行写文件和执行命令：

```powershell
agent-client --workspace D:/code/my-project --allow-write --allow-commands run "Fix the failing test and verify the change"
```

默认 `ask` 模式下，非交互输入无法回答审批时会拒绝工具。上述两个授权选项不会批准 MCP 远端工具调用。需要本次运行跳过全部审批时，使用全局选项 `--approval-mode never`；只允许读取时使用 `--approval-mode read_only`。

执行结束关注 `status` 和 `stop_reason`。`completed` 表示本次运行完成；`partial`、`failed` 或 `cancelled` 需要检查原因。命令退出码非零不等于文件没有发生修改。

## 5. 个人配置与模型

默认配置文件位于：

```text
~/.agent-client/config.toml
```

可以用记事本打开：

```powershell
notepad "$env:USERPROFILE\.agent-client\config.toml"
```

主要配置段是 `[model]`、`[runtime]`、`[context]`、`[skills]` 和 `[mcp]`。完整有效示例见 [config.example.toml](../config.example.toml)。修改已有段落中的字段，不要重复创建同名 TOML 段落；保存后重启客户端。

`provider`、`auth_mode`、`base_url`、`model` 和 `api_key_env` 是认证方式与请求配置的标识，客户端启动时据此选择接口。登录结果中的实际令牌由认证模块保存在 `.agent-client/auth/credentials.bin`，Windows 使用当前用户 DPAPI 加密；配置文件不保存令牌或 `logged_in` 布尔值。账户状态以实际凭据检查为准。API key 配置只引用环境变量名。

切换 DeepSeek 时，将默认文件中整个 `[model]` 段替换为下面内容，保留 `[runtime]`、`[context]`、`[skills]` 和 `[mcp]` 等其他段：

```toml
[model]
provider = "openai_chat_completions"
auth_mode = "api_key"
api_key_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
reasoning_effort = "high"
reasoning_levels = ["none", "high", "max"]
chat_reasoning = "enabled"
chat_send_reasoning_effort = true
chat_stream_usage = true
context_window = 256000
max_output_tokens = 8192
```

保存后直接运行 `agent-client`；不需要 `--config`，也不需要浏览器登录。它在当前工作目录新建会话，工作目录不合适时先 `cd` 到项目目录。切回订阅时，将整个 `[model]` 段替换为以下内容，去掉 DeepSeek 专属字段：

```toml
[model]
provider = "openai_responses"
auth_mode = "chatgpt"
base_url = "https://api.openai.com/v1"
model = "gpt-6.1-sol"
reasoning_effort = "medium"
context_window = 256000
max_output_tokens = 8192
```

重新运行 `agent-client` 会使用已保存的订阅凭据，需要重新授权时执行 `agent-client auth login`。`auth login` 按当前配置进行认证，不擅自改写用户选择的端点、模型或其他配置。

| 字段 | 用途 |
| --- | --- |
| `model.model` | 模型名称，须在账户可用范围内 |
| `model.reasoning_effort` | 推理强度，当前默认 `medium` |
| `model.reasoning_levels` | 当前配置可选强度；DeepSeek 示例为 `none`、`high`、`max` |
| `model.context_window` | 本地上下文预算，按所选模型能力设置 |
| `runtime.max_model_steps` | 每次 Run 的模型调用步数上限 |
| `runtime.max_tool_calls` | 每次 Run 的工具调用数上限 |
| `runtime.deadline_seconds` | 活动运行时限 |
| `runtime.allow_write` / `allow_commands` | 是否默认允许本地写入和命令；当前均为 `false` |
| `runtime.approval_mode` | `ask`（默认）、`never` 或 `read_only` |

`/model MODEL_NAME` 只改变当前进程的模型设置，不写回配置。`/compact` 使用同一个已授权模型生成摘要，因此会调用模型。订阅模式下，`max_output_tokens` 参与本地预算，但当前不会作为请求参数发送给订阅服务。

`/reasoning` 在输入框上方打开选择菜单，或用 `/reasoning high` 直接设置；只对当前进程生效，不写回配置，运行期间不能切换。启动时可用全局 `--reasoning-effort high` 覆盖。菜单范围由 `reasoning_levels` 决定，非法选择立即报错。DeepSeek 配置中 `none` 关闭 thinking，`high` / `max` 启用对应强度。通用 Chat Completions 示例默认不发送 `thinking` 或 `reasoning_effort` 附加字段；显式选择强度会启用 effort 参数，服务必须支持该字段。

配置示例分别见 [DeepSeek](../config.deepseek.example.toml) 和 [通用 OpenAI 兼容服务](../config.openai-compatible.example.toml)。会话历史绑定 provider、endpoint 和认证模式；更换这三项后应新建会话，不能把旧会话直接续作到另一服务。

旧版会话缺少 endpoint 或认证绑定时，只允许原默认官方 Responses + ChatGPT 组合续作或压缩；使用 DeepSeek 配置请新建会话。通用 chat 的 `chat_reasoning = "default"` 无法解释 `none`，与 `reasoning_effort = "none"` 同时配置会立即报错；需像 DeepSeek 示例一样配置服务支持的显式 thinking 开关。

只有临时使用另一份配置或隔离数据时，才需要显式参数；日常入口仍是 `agent-client`：

```powershell
agent-client --config D:/agent-configs/personal.toml --workspace D:/code/my-project
agent-client --home D:/agent-data/separate auth login
agent-client --home D:/agent-data/separate --workspace D:/code/my-project
```

自定义数据目录的每次命令都要使用同一个 `--home`。配置文件写错或显式指定的文件不存在时，会直接报错。

## 6. 接入个人 Skills 和 MCP

在个人配置已有的 `[skills]` 段设置目录，然后重启客户端：

```toml
[skills]
roots = ["~/.agents/skills"]
catalog_token_budget = 2000
```

目录中的 Skill 应包含 `SKILL.md`。用 `/skills` 查看是否被发现。个人通用指令可以放在 `~/.agent-client/AGENTS.md`，项目规则放在对应项目的 `AGENTS.md`。安装过程没有自动启用你的全部 Skills 或额外 MCP 服务。

下面是 MCP 配置模板，需要先准备相应服务，再替换命令或地址：

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

`env` 左边是子进程变量名，右边是主机已有环境变量的名称；`token_env` 也填写变量名。不要把实际密钥写入 TOML。MCP 优先读取当前进程的指定变量；Windows 下若进程没有该变量，则读取已保存的用户变量，再读取系统变量。显式存在但为空的变量会报错，不会被另一来源覆盖。无需为了刷新 MCP 变量而重启整个终端宿主，重启 agent-client 即可。

启动时会显示 MCP 已连接服务或具体连接错误，也可用 `/mcp` 查看详情。模型能看到服务连接状态；`search_mcp_tools` 返回工具列表和连接状态，空列表不再掩盖认证问题。搜索使用短关键词，空查询列出全部可用工具；长句要求全部词匹配，可能没有结果。`required=true` 的服务连接失败会阻止启动。当前支持工具、资源和 prompts，未接入 MCP OAuth、elicitation、sampling 或 roots 回调。

MCP 暂时断线不会锁住聊天。明确声明只读的查询超时记为失败，其他结果未知的操作保留 UNKNOWN。下一次新请求可重新连接，不会自动重放之前超时的请求。工具尚未派发时显示 queued，实际开始执行才显示 working。

## 7. 错误处理

| 现象 | 处理 |
| --- | --- |
| 找不到 `agent-client` 或 `rg` | 重新打开 PowerShell，执行 `Get-Command agent-client,rg` |
| `signed_out` / `reauth_required` | 执行 `agent-client auth login`，成功后继续原会话 |
| 模型无权访问 | 执行 `auth models` 查看目录，调整模型 |
| 额度耗尽 | 等账户额度恢复或处理账户限制；不会自动切 API key |
| `partial` / `model_budget` / `tool_budget` | 检查进度，必要时调整预算，再 `continue` |
| `session_busy` | 关闭正在占用会话或数据目录的客户端；维护命令要求客户端退出 |
| 文件 hash 冲突 | 文件已被其他操作修改，重新读取并重新规划修改 |
| `unknown_outcome` / `UNKNOWN` | 仍能发消息和执行允许的只读查询；核验后 `resolve` 才能继续副作用操作，不能直接重跑 |
| SQLite 损坏 | 关闭客户端，执行 `rebuild`；保留原始会话文件 |
| JSONL 中间记录损坏 | 停止恢复，保留现场并使用有效备份；不要删坏行后假装历史完整 |

处理 UNKNOWN 时，先检查实际文件、命令或远端服务，再选择符合事实的结果并写明依据：

```powershell
agent-client resolve SESSION_ID CALL_ID succeeded "Confirmed the resulting file and external receipt"
agent-client continue SESSION_ID
```

若核验结果为未完成，把 `succeeded` 改为 `failed`。`resolve` 只保存结论，不替你执行工具。诊断入口是 `agent-client diagnostics`，默认日志位于 `~/.agent-client/logs/runtime.jsonl`。

## 8. 备份、重建和删除

先停止运行，再把会话备份到数据目录之外的新目录：

```powershell
agent-client backup D:/agent-backups/snapshot-001
```

备份保存完整有效的日志前缀、artifacts、配置和边界 manifest，不包含账户凭据，也不是项目代码备份。

SQLite 是可重建投影。客户端全部退出后，可以执行：

```powershell
agent-client rebuild
```

重建前会保留旧 SQLite 及 sidecar 文件，输出备份位置；它不调用模型或重放工具。JSONL 本身损坏时不能依靠重建补造事实。

恢复外部备份时，把 `sessions` 和可选配置复制到一个独立数据目录，再重建、重新登录和检查会话：

```powershell
agent-client --home D:/agent-restored rebuild
agent-client --home D:/agent-restored auth login
agent-client --home D:/agent-restored sessions
agent-client --home D:/agent-restored resume SESSION_ID
```

显式删除会话：

```powershell
agent-client delete SESSION_ID
```

删除会把会话保留在输出的 `trash_directory` 中，并留下删除标记。需要找回时，将其中的 `session` 目录复制到独立数据目录下的 `sessions/SESSION_ID` 后重建。不要直接放回原数据目录，它会再次被隔离。会话删除不会删除项目代码或账户。

## 9. 更新和卸载

更新前退出正在运行的客户端，从 [Releases](https://github.com/ShiqinGuo/e/releases) 下载新版 wheel，将下面的 `VERSION` 替换为下载的版本号：

```powershell
uv tool install --force --python 3.12 ./agent_client-VERSION-py3-none-any.whl
agent-client --help
```

查看工具环境和命令位置：

```powershell
uv tool dir
Get-Command agent-client
```

从源码更新时，在仓库根目录执行以下命令，以锁文件中的依赖版本重新安装：

```powershell
$agentInstallWork = Join-Path ([System.IO.Path]::GetTempPath()) ("agent-client-install-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Force -Path $agentInstallWork | Out-Null
uv export --locked --no-dev --no-emit-project --no-hashes --no-annotate --no-header --output-file "$agentInstallWork/runtime-constraints.txt"
uv tool install --force --reinstall-package agent-client --python 3.12 --constraints "$agentInstallWork/runtime-constraints.txt" .
agent-client --help
```

普通安装不会随源码修改自动更新。卸载使用 `uv tool uninstall agent-client`；卸载程序会保留个人配置、账户和会话。
