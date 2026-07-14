# VS Code Claude Code：Anthropic 官方模式

> 需要在官方 Claude 与 Gemini 之间切换时，使用
> [VS Code Claude Code 模式切换](VSCODE_CLAUDE_MODES.md)。

该模式让 VS Code 的 Claude Code 插件继续使用 Anthropic 官方账号登录或官方 API，仅把 API 网络传输交给 FanVPN Bridge。它不需要 CC Switch，也不会修改 `~/.claude/settings.json`。

链路如下：

```text
VS Code Claude Code
  -> http://127.0.0.1:18888/anthropic
  -> FanVPN Bridge
  -> Chrome / FanVPN
  -> https://api.anthropic.com
```

## 自动配置

关闭敏感任务后，在 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_vscode_claude_official.ps1
```

脚本只给 VS Code Claude Code 设置 `ANTHROPIC_BASE_URL`，同时移除插件设置中可能覆盖官方账号登录的 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN` 和 `CLAUDE_CODE_OAUTH_TOKEN`。原 `settings.json` 会先备份。

运行后在 VS Code 命令面板执行 **Developer: Reload Window**，打开 Claude Code 面板并选择官方登录。浏览器授权页面由用户正常完成；Bridge 不读取浏览器 Cookie 或登录凭据。

## 官方账号登录

官方账号登录不应设置 `ANTHROPIC_API_KEY`。Claude Code 使用自己保存的 OAuth 凭据，Bridge 透明转发请求头和 Anthropic Messages 流。

## Anthropic API Key

如需使用 Anthropic Console API Key，可在插件配置中额外加入 `ANTHROPIC_API_KEY`。API Key 的优先级高于官方订阅账号登录，因此两种方式不要同时配置。

## 恢复

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_vscode_claude_official.ps1 -Undo
```

恢复后再次重载 VS Code 窗口。

## 与 Gemini 模式的边界

本模式不做协议转换。只有让 Claude Code 使用 Gemini API 时，才启用 CC Switch 完成 Anthropic Messages 到 Gemini Native 的转换和 `thoughtSignature` 状态维护。
