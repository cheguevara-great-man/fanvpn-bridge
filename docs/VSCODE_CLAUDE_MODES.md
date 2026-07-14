# VS Code Claude Code 模式切换

`set_vscode_claude_mode.ps1` 只修改 VS Code 用户设置中的
`claudeCode.environmentVariables`。它不会让全局 Claude CLI 或 Claude 客户端走
FanVPN Bridge；如果 CC Switch 启动时写入了 `~/.claude/settings.json`，脚本只移除
CC Switch 自己的 `PROXY_MANAGED` 接管值，并保留其他用户配置。

## Gemini 模式

首次配置 Gemini 供应商时，把 Key 临时放入当前 PowerShell 进程并写入 CC Switch
数据库：

```powershell
$env:GEMINI_API_KEY = '你的 Gemini API Key'
node .\tools\configure_ccswitch_gemini.mjs --apply
Remove-Item Env:GEMINI_API_KEY
```

之后切换 VS Code 插件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set_vscode_claude_mode.ps1 -Mode Gemini
```

链路是：

```text
VS Code Claude Code
  -> CC Switch (127.0.0.1:15721)
  -> Anthropic Messages 到 Gemini Native 的转换
  -> FanVPN Bridge (127.0.0.1:18888/gemini)
  -> Chrome / FanVPN
  -> Google Gemini API
```

CC Switch 负责模型映射、工具调用转换和 `thoughtSignature` 回放。Bridge 只传输
HTTP。若 Google 返回 `User location is not supported for the API use`，说明当前
FanVPN 节点不支持 Gemini API，需要在 Chrome 的 FanVPN 扩展中切换节点；浏览器
能打开 ChatGPT 并不代表该节点一定支持 Gemini API。

## Anthropic 官方模式

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set_vscode_claude_mode.ps1 -Mode Official
```

该模式让 VS Code Claude Code 使用官方 Claude.ai 登录或 Anthropic API Key，网络
经 `127.0.0.1:18888/anthropic` 转发，不经过 CC Switch。

## 使设置生效

每次切换后，在 VS Code 命令面板执行 `Developer: Reload Window`。重启 Chrome 不会
自动切换 Claude 模式；Chrome/FanVPN 只负责 Bridge 的浏览器出口。
