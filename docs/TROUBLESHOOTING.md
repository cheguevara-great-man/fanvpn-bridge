# 故障排查

## 一键诊断

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\diagnose.ps1
```

诊断结果会比较源码、Chrome 注册产物和当前运行进程的 EXE 路径、route 名称与 route 文件 SHA-256，
只输出不含凭据的元数据。自动化检查可加 `-Strict`；发现缺失文件、版本错位或 Bridge 不可用时返回非零退出码。

再检查三个本地端点：

```powershell
Invoke-RestMethod http://127.0.0.1:18888/health -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/routes -Proxy $null
```

`/health` 表示 Host 正在运行；`/ready` 还要求 Native Messaging 和 Offscreen
执行器都已连接。

## 常见问题

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `127.0.0.1:18888` 拒绝连接 | Chrome、扩展或 Native Host 未启动 | 打开 Chrome，刷新扩展；必要时重新运行 `install.ps1` |
| 扩展显示 Native Host 未连接 | 注册路径、构建目录或扩展 ID 不一致 | 运行 `diagnose.ps1`，重新构建并注册 |
| `/health` 正常但 `/ready` 失败 | Offscreen 尚未就绪或扩展通道断开 | 刷新 FanVPN AI Bridge，查看扩展错误 |
| ChatGPT 网页可开，但 Codex route 返回 502 | Chrome 扣留 Bridge 的站点权限 | 将 FanVPN AI Bridge 的网站访问设为“在所有网站上” |
| Codex 登录 Token Exchange 返回地区 403 | Token Exchange 由本地 Codex 进程直连 `auth.openai.com` | 关闭 VS Code，按使用指南运行一次性 Codex 登录助手 |
| 更新后新路由没有出现 | Chrome 仍在运行切换前的 Host，或扩展尚未刷新 | 刷新扩展并重开 Chrome，再检查 `/ready` 和 `/routes` |
| `source_matches_registered` 为 `false` | 当前注册的 route 配置与仓库源码不同 | 确认本地改动后运行 A/B 更新脚本，刷新扩展并重开 Chrome |
| `registered_matches_running` 为 `false` | 注册已切换，但 Chrome 仍保持旧 Host 进程 | 关闭并重新打开 Chrome |
| A/B 更新报 `WinError 5` 或 `libcrypto-3.dll` 拒绝访问 | 上次切换后未重开 Chrome，旧进程仍占用本次目标槽位 | 关闭 Chrome，确认所有 Host 进程退出后重新运行更新脚本 |
| `Failed to fetch` / `UPSTREAM_CONNECTION_FAILED` | FanVPN 未开启、节点失效或目标被节点限制 | 在同一 Chrome profile 检查 FanVPN 并切换节点 |
| 本地请求被发送到 Clash/系统代理 | `NO_PROXY` 未包含 loopback，或应用未重启 | 添加 `127.0.0.1,localhost` 后重启 VS Code |
| 请求长时间无首个流式输出 | 扩展版本旧或未刷新 | 刷新 FanVPN AI Bridge，确认已加载当前 `stream.js` |
| Gemini 400 提到 signature | CC Switch 协议转换器不匹配 | 使用带 Gemini 3 `thoughtSignature` 修复的 CC Switch 构建 |
| Gemini 返回地区不支持 | 当前 FanVPN 节点不支持 Gemini API | 切换 FanVPN 节点；能打开 ChatGPT 不代表 Gemini API 可用 |
| Claude Official 模式仍要求自定义 Token | VS Code 中残留认证环境变量 | 运行 `set_vscode_claude_mode.ps1 -Mode Official` 并 Reload Window |
| Gemini 模式不经过 CC Switch | VS Code 未重载或 15721 未监听 | 运行 Gemini 模式脚本，检查 CC Switch，再 Reload Window |
| 全局 Claude CLI 被 CC 接管 | CC Switch 启动时写入 `~/.claude/settings.json` | 再运行任一 VS Code Claude 模式脚本，清除 `PROXY_MANAGED` 接管 |

## Chrome 站点权限

Chrome 可能在扩展重装、升级或策略变化后扣留 Manifest 的 `https://*/*` 权限。
在 `chrome://extensions` 打开 FanVPN AI Bridge 详情，确认“网站访问权限”为
“在所有网站上”。这是 Bridge 扩展的权限，不是 FanVPN 节点开关。

## Windows 登录后未自动恢复

```powershell
Get-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap'
Get-ScheduledTaskInfo -TaskName 'FanVPN Bridge Bootstrap'
Start-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap'
```

自动任务只能启动 Chrome 和等待 Bridge；它不能替用户打开 FanVPN、选择节点或
授权 Chrome 扩展。

启动日志：

```text
%LOCALAPPDATA%\FanVPNBridge\startup.log
%LOCALAPPDATA%\FanVPNBridge\fanvpn-bridge.log
```

## 安全地重启 Bridge

1. 关闭 Chrome。
2. 确认本仓库构建目录中的 `browser-ai-bridge.exe` 已退出。
3. 重新打开 Chrome，或运行 `Start-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap'`。
4. 等待 `/ready` 返回 200。

停止计划任务不会终止已经由 Chrome 启动的 Native Host。

## 区分问题所在层

- **本地层**：18888 无法连接，检查安装、进程和注册表。
- **Chrome 层**：`health` 正常但 `ready` 失败，检查扩展和 Offscreen。
- **FanVPN 层**：Bridge ready，但所有外网 route 都连接失败，检查节点。
- **上游层**：收到真实 4xx/5xx，检查凭据、地区、配额或模型名。
- **转换层**：只有 Claude→Gemini 工具调用失败，检查 CC Switch。
- **客户端层**：Bridge 请求成功但 UI/侧栏异常，检查 Codex 或 Claude 客户端；Bridge 不管理聊天列表。

## Codex 第一条消息明显较慢

Native Host 会为真实上游请求记录不含 URL、query、正文和认证信息的分段耗时。先复现一条慢消息，
再在 PowerShell 中查看最近的 Codex 请求：

```powershell
Get-Content "$env:LOCALAPPDATA\FanVPNBridge\fanvpn-bridge.log" -Tail 500 |
  Select-String 'request_(complete|failed) route=(chatgpt-codex|auth-openai)'
```

`response_head_ms` 表示浏览器取得上游 HTTP 响应头的时间，`first_body_ms` 表示收到第一段
响应数据的时间，`total_ms` 表示流结束时间。这样可以区分代理/TLS 建连慢、认证刷新慢和模型首段
输出慢；日志不会包含 Token 或提示词。

开发问题的根因和修复背景见[问题与解决记录](PROBLEM_SOLVING.md)。
