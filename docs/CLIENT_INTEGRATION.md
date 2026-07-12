# 客户端接入

## 先选对链路

FanVPN Bridge 是受 allowlist 约束的反向 HTTP 网关，不是 `CONNECT`/SOCKS 代理。不要把它填进 CC Switch 的“全局代理”输入框；应把具体供应商的 API Endpoint 指向 Bridge route。

### Claude Code 直连 Anthropic

把 `ANTHROPIC_BASE_URL` 设为：

```text
http://127.0.0.1:18888/anthropic
```

认证仍由 Claude Code 的 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` 提供。示例见 `config/claude-fanvpn.example.json`。

### Claude Code 经 CC Switch 使用 Gemini

这是 Gemini 3 多轮工具调用的主链路：

```text
Claude Code
  -> CC Switch local proxy (http://127.0.0.1:15721)
  -> Anthropic-to-Gemini conversion + thoughtSignature state
  -> http://127.0.0.1:18888/gemini
  -> Chrome Offscreen fetch
  -> FanVPN
  -> generativelanguage.googleapis.com
```

在 CC Switch 的 Claude 供应商中使用：

- API Endpoint：`http://127.0.0.1:18888/gemini`
- API 格式：`Gemini Native generateContent`
- API Key：Google Gemini key
- 模型：例如 `gemini-3.5-flash`

然后启动 CC Switch 本地代理，并开启 Claude 应用接管。Bridge 不解析 JSON，也不生成、缓存或修改 `thoughtSignature`。

### Codex 直连 OpenAI

Codex 自定义 provider 只支持 Responses wire API。示例见 `config/codex-fanvpn.example.toml`：复制为 `$CODEX_HOME/fanvpn.config.toml`，设置 `OPENAI_API_KEY` 后用 `codex --profile fanvpn` 启动。

该方式刻意不修改正常的 `~/.codex/config.toml`，因此不会破坏现有 ChatGPT 登录。VS Code 扩展若要永久切换到 API provider，应由 CC Switch 接管或在确认 API Key 可用后再修改用户级配置。

## 验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\verify_routes.ps1

# 若当前进程已有 GEMINI_API_KEY，则同时验证真实鉴权
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\verify_routes.ps1 -TestGemini
```

脚本不输出 API Key，也不把它写入文件。
