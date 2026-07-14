# 客户端接入

## 先选对链路

FanVPN Bridge 是受 allowlist 约束的反向 HTTP 网关，不是 `CONNECT`/SOCKS 代理。不要把它填进 CC Switch 的“全局代理”输入框；应把具体供应商的 API Endpoint 指向 Bridge route。

### Claude Code 直连 Anthropic

把 `ANTHROPIC_BASE_URL` 设为：

```text
http://127.0.0.1:18888/anthropic
```

认证仍由 Claude Code 自己提供：可以使用 Claude.ai 官方账号登录，也可以使用 Anthropic Console 的 `ANTHROPIC_API_KEY`。官方账号登录时不要设置 API Key 或自定义认证 Token，否则环境变量会优先于已保存的 OAuth 登录。

VS Code 插件推荐使用只影响该插件的配置：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_vscode_claude_official.ps1
```

该脚本不会修改 `~/.claude/settings.json`，也不会启动 CC Switch。完整说明见 [VS Code Claude Code：Anthropic 官方模式](VSCODE_CLAUDE_OFFICIAL.md)。通用 Claude Code 配置示例见 `config/claude-fanvpn.example.json`。

### Claude Code 经 CC Switch 使用 Gemini

只有 Claude Code 使用 Gemini API 时才需要这条链路。CC Switch 负责协议转换和 Gemini 3 多轮工具调用状态：

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

然后使用 VS Code 专用模式脚本启动 CC Switch 本地代理并配置插件。不要开启全局 Claude 应用接管；Bridge 不解析 JSON，也不生成、缓存或修改 `thoughtSignature`。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set_vscode_claude_mode.ps1 -Mode Gemini
```

也可以关闭 CC Switch 后，用 Node.js 22+ 的脚本完成同样的可回滚配置：

```powershell
# 先预览；Key 只从当前进程环境读取，不会打印
node .\tools\configure_ccswitch_gemini.mjs

# 自动备份 ~/.cc-switch/cc-switch.db 后应用
node .\tools\configure_ccswitch_gemini.mjs --apply
```

脚本会创建 `Gemini Native via FanVPN` 供应商、设为当前 Claude 供应商，并启用 `127.0.0.1:15721` 的协议转换代理。它不修改 `~/.claude/settings.json`。可用 `node .\tools\inspect_ccswitch_db.mjs --summary` 查看脱敏后的生效状态。完整的 VS Code 专用切换流程见 [VS Code Claude Code 模式切换](VSCODE_CLAUDE_MODES.md)。

Gemini route 只向浏览器转发必要请求头。Claude Code 和 SDK 产生的 `anthropic-*`、`x-app`、`x-stainless-*` 元数据头会触发 Google 的跨域预检失败，因此不会送到 Google；认证头、内容类型和流式响应保持不变。

### Codex 直连 OpenAI

Codex 自定义 provider 只支持 Responses wire API。示例见 `config/codex-fanvpn.example.toml`：复制为 `$CODEX_HOME/fanvpn.config.toml`，设置 `OPENAI_API_KEY` 后用 `codex --profile fanvpn` 启动。

该方式刻意不修改正常的 `~/.codex/config.toml`，因此不会破坏现有 ChatGPT 登录。VS Code 扩展若要永久切换到 API provider，应由 CC Switch 接管或在确认 API Key 可用后再修改用户级配置。

### Codex 使用现有 ChatGPT 登录

Codex 的 ChatGPT 订阅后端可以通过独立 route 转发。配置见 `config/codex-fanvpn-chatgpt.example.toml`。建议先把它复制为 `$CODEX_HOME/fanvpn-chatgpt.config.toml`，用 `codex --profile fanvpn-chatgpt` 验证，再决定是否合并进默认用户配置。

这里使用 `requires_openai_auth = true` 复用现有 ChatGPT 登录，并显式设置 `supports_websockets = false`。Bridge 当前传输 HTTP/SSE；关闭 WebSocket 可避免客户端先重试 WebSocket 再回退 HTTP 的延迟。该 route 的固定上游是 `https://chatgpt.com/backend-api/codex`，不能被客户端改成其他主机。

## 验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\verify_routes.ps1

# 若当前进程已有 GEMINI_API_KEY，则同时验证真实鉴权
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\verify_routes.ps1 -TestGemini

# 发起一次真实 Gemini Native SSE，并覆盖浏览器不兼容请求头的回归测试
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\verify_routes.ps1 -TestGeminiStream
```

脚本不输出 API Key，也不把它写入文件。
