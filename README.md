# e

**Agent Client · ShaneGuo** — 手写异步 Python 编码 Agent，提供终端界面和命令行。

支持 ChatGPT 订阅登录、DeepSeek / OpenAI 兼容 API、本地编码工具、个人 Skills、MCP、上下文压缩，以及基于 JSONL 和 SQLite 的会话恢复。

## 安装

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [ripgrep](https://github.com/BurntSushi/ripgrep#installation)。

从 [Releases](https://github.com/ShiqinGuo/e/releases) 下载 wheel，或直接安装当前版本：

```shell
uv tool install --python 3.12 https://github.com/ShiqinGuo/e/releases/download/v0.1.16/agent_client-0.1.16-py3-none-any.whl
```

## 使用

```shell
agent-client auth login
agent-client
```

默认通过 ChatGPT 登录。使用其他模型时，修改 `~/.agent-client/config.toml` 并重启；示例见 [DeepSeek](config.deepseek.example.toml) 和 [OpenAI 兼容服务](config.openai-compatible.example.toml)。

Enter 发送，运行中 Enter 提交 steer、Tab 排队，Shift+Enter 换行，Esc 取消，Ctrl+Q 退出。输入 `/` 查看命令。

## 文档与开发

- [简报运行示例](docs/RUN.md)
- [操作指南](docs/USER_GUIDE.md)
- [设计文档](docs/DESIGN.md)
- [工程约定](AGENTS.md)

在源码目录执行：

```shell
uv sync --locked --python 3.12
uv run agent-client
```
