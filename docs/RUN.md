# 快速运行

需要 Python 3.12+、uv 和 ripgrep。

```powershell
uv tool install --python 3.12 https://github.com/ShiqinGuo/e/releases/download/v0.1.16/agent_client-0.1.16-py3-none-any.whl
git clone https://github.com/ShiqinGuo/e.git
cd e/examples/mining
$env:MINING_SERVICE_TOKEN = '填入交付 RUN.md 中的测试 Token'
```

## ChatGPT

```powershell
agent-client --config ./config-chatgpt.toml auth login
agent-client --config ./config-chatgpt.toml run '给我生成一份关于 Pilbara 锂矿的今日简报'
```

## DeepSeek

```powershell
$env:DEEPSEEK_API_KEY = '你的 DeepSeek API key'
agent-client --config ./config-deepseek.toml run '给我生成一份关于 Pilbara 锂矿的今日简报'
```

配置示例：[ChatGPT](../examples/mining/config-chatgpt.toml)、[DeepSeek](../examples/mining/config-deepseek.toml)。业务指引为 [mining-brief Skill](../examples/mining/skills/mining-brief/SKILL.md)。

## Claude Code

```powershell
$env:MCP_SDK_GENERATION = 'v2'
$env:MCP_PROTOCOL_NEGOTIATION = 'auto'
New-Item -ItemType Directory -Force .claude/skills/mining-brief | Out-Null
Copy-Item ./skills/mining-brief/SKILL.md .claude/skills/mining-brief/SKILL.md
claude --mcp-config ./mcp-config.json --strict-mcp-config --allowedTools 'Skill,Read,Bash,mcp__mining-news__*,mcp__mining-documents__*,mcp__mining-market__*' -p '给我生成一份关于 Pilbara 锂矿的今日简报'
```

服务端与本地部署：[mining-intelligence-server](https://github.com/ShiqinGuo/mining-intelligence-server)。
