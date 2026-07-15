# 客户端使用

## 使用前检查

```powershell
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
```

只有 `ready=true` 时才配置客户端。FanVPN Bridge 是带 allowlist 的反向 HTTP 网关，
不是通用代理；不要把 `127.0.0.1:18888` 填入系统代理或 CC Switch 的“全局代理”。

## Codex 使用 OpenAI API Key

示例位于 [`config/codex-fanvpn.example.toml`](../config/codex-fanvpn.example.toml)。将它复制到独立的 Codex 配置位置，
设置 `OPENAI_API_KEY` 后使用对应 profile。核心 provider 配置是：

```toml
[model_providers.fanvpn_openai]
base_url = "http://127.0.0.1:18888/openai/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
supports_websockets = false
```

不要直接覆盖正常的 `~/.codex/config.toml`，除非明确希望 API Key 模式替代当前
ChatGPT 登录。

## Codex 使用 ChatGPT 登录

示例位于 [`config/codex-fanvpn-chatgpt.example.toml`](../config/codex-fanvpn-chatgpt.example.toml)：

```toml
[model_providers.fanvpn_chatgpt]
base_url = "http://127.0.0.1:18888/chatgpt-codex"
requires_openai_auth = true
wire_api = "responses"
supports_websockets = false
```

该 route 固定转发到 ChatGPT Codex backend。Bridge 不读取浏览器 Cookie，而是转发
Codex 自己已有的认证请求头。Bridge 当前不传输 WebSocket，因此必须关闭 WebSocket。

### 在当前电脑独立登录

Codex IDE 与 CLI 共用本机的登录缓存。普通登录会让本地 Codex 进程直接执行 Token
Exchange；在受地区限制的网络中，即使 Chrome 登录网页能够打开，这一步仍可能返回
403。项目提供一次性登录助手，让授权网页和 Token Exchange 都使用 Chrome 当前出口，
不需要复制另一台电脑的登录文件。

先确认 Chrome 中的代理扩展已连接，并关闭所有 VS Code 窗口。然后在仓库目录的
PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\codex_login_via_bridge.ps1
```

脚本会打开 Chrome 官方登录页。完成登录后，浏览器回到只监听本机的临时回调端口，
助手校验 PKCE 和 `state`，再通过 `127.0.0.1:18888/auth-openai/oauth/token`
换取凭据。成功后它会：

- 把凭据写入当前 Windows 用户的 `~/.codex/auth.json`；
- 如果原文件存在，先在同一目录创建带时间戳的备份；
- 自动设置后续刷新和注销所需的两个用户环境变量；
- 不在控制台或 Bridge 日志中打印 Token。

Windows 中 `~/.codex/auth.json` 实际位于：

```text
C:\Users\<你的 Windows 用户名>\.codex\auth.json
```

只需查看位置时可运行 `explorer.exe "$HOME\.codex"`。该文件等同密码，不要复制到
项目、提交到 Git、上传网盘或粘贴到聊天中。登录助手完成后重新打开 VS Code。

如果脚本提示当前 Host 不包含 `auth-openai`，先按[安装文档](INSTALLATION.md)更新
Native Host，并刷新 Chrome 扩展。

当前电脑的 `~/.codex/config.toml` 需要选择 ChatGPT Bridge provider。建议先备份原文件，再复制
[`config/codex-fanvpn-chatgpt.example.toml`](../config/codex-fanvpn-chatgpt.example.toml)：

```powershell
Copy-Item "$HOME\.codex\config.toml" "$HOME\.codex\config.toml.before-browser-bridge.bak" -ErrorAction SilentlyContinue
Copy-Item ".\config\codex-fanvpn-chatgpt.example.toml" "$HOME\.codex\config.toml" -Force
```

最终关键配置应为：

```toml
model_provider = "fanvpn_chatgpt"

[model_providers.fanvpn_chatgpt]
base_url = "http://127.0.0.1:18888/chatgpt-codex"
requires_openai_auth = true
wire_api = "responses"
supports_websockets = false
```

完成登录和配置后关闭所有 VS Code 窗口再重新打开。Codex IDE 会读取
`~/.codex/auth.json`。成功判据是：Codex 直接进入聊天页，并能在关闭 Clash 的情况下
完成一次真实对话。

## VS Code Claude Code：Anthropic 官方模式

该模式使用 Claude.ai 官方登录或 Anthropic API Key，不经过 CC Switch：

```text
VS Code Claude Code
  -> 127.0.0.1:18888/anthropic
  -> Chrome / FanVPN
  -> api.anthropic.com
```

配置：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\set_vscode_claude_mode.ps1 -Mode Official
```

脚本只修改 VS Code 用户设置中的 `claudeCode.environmentVariables`，移除会覆盖
官方登录的托管 API Key/Token，并保留其他 VS Code 设置。完成后在 VS Code 命令
面板执行 `Developer: Reload Window`。

使用官方账号登录时不要同时设置 `ANTHROPIC_API_KEY`；环境变量认证优先于已保存的 OAuth 登录。

## VS Code Claude Code：Gemini 模式

该模式需要 CC Switch：

```text
VS Code Claude Code
  -> CC Switch · 127.0.0.1:15721
  -> Anthropic Messages → Gemini Native
  -> 127.0.0.1:18888/gemini
  -> Chrome / FanVPN
  -> generativelanguage.googleapis.com
```

### CC Switch 供应商配置

在 CC Switch 的 Claude 应用中配置：

| 字段 | 值 |
|---|---|
| 名称 | `Gemini Native via FanVPN` |
| API Endpoint | `http://127.0.0.1:18888/gemini` |
| API 格式 | Gemini Native / `generateContent` |
| API Key | 你的 Google Gemini API Key |
| 模型 | 可用的 Gemini 模型，例如 `gemini-3.5-flash` |
| 本地代理 | `127.0.0.1:15721` |

也可用脚本写入同等配置。Key 只从当前进程环境读取，不会打印：

```powershell
$env:GEMINI_API_KEY = '你的 Gemini API Key'
node .\tools\configure_ccswitch_gemini.mjs --apply
Remove-Item Env:GEMINI_API_KEY
```

然后切换 VS Code：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\set_vscode_claude_mode.ps1 -Mode Gemini
```

脚本会确保 CC Switch 本地代理正在监听，并让 VS Code Claude 插件单独连接它。
如果 CC Switch 启动时写入了全局 `~/.claude/settings.json`，脚本只移除
`PROXY_MANAGED` 接管值；全局 Claude CLI/客户端不会被这套模式接管。

切换后执行 `Developer: Reload Window`。CC Switch 只影响指向 `127.0.0.1:15721`
的请求；Official 模式、Codex 和其他 VS Code 扩展不会经过它。

## Claude 模式切换总结

```powershell
# Claude 官方登录/API
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\set_vscode_claude_mode.ps1 -Mode Official

# Claude Code 使用 Gemini
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\set_vscode_claude_mode.ps1 -Mode Gemini
```

重启 Chrome 不会改变 VS Code 的 Claude 模式。模式由 VS Code 用户设置决定；
Chrome 和 FanVPN 只提供网络出口。

## 路由验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\verify_routes.ps1
```

真实验证 Gemini：

```powershell
$env:GEMINI_API_KEY = '你的 Gemini API Key'
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\verify_routes.ps1 -TestGemini -TestGeminiStream
Remove-Item Env:GEMINI_API_KEY
```

验证脚本不会输出 Key，也不会将 Key 写入仓库。
