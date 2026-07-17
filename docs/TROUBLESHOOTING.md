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
| A/B 更新报 `WinError 5` 或 `libcrypto-3.dll` 拒绝访问 | 非活动槽残留旧 Host，或安全软件正在扫描 DLL | 2.6.3+ 会自动结束非活动槽残留进程；等待数秒重试，仍失败时再暂时关闭 Chrome |
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

自动任务会无窗口启动和持续监测 Chrome/Bridge；它不能替用户打开 FanVPN、选择节点或
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

Codex 的自定义 `model_provider` 只控制模型目录和 Responses 对话。启动时，客户端还可能初始化
Apps、已安装插件同步、远程插件目录和分析服务；没有系统代理时，这些产品后端请求会先超时，
所以第一条消息慢，而同一进程中的后续消息较快。

默认 Browser Lean 采用精简配置：

```toml
[features]
apps = false
plugins = false
remote_plugin = false

[analytics]
enabled = false
```

使用模式切换脚本时这些值会自动写入，并在切换到 Browser Full 或 Direct 时恢复原值。Lean 下
`~/.codex/config.toml` 不应指向 `chatgpt-backend`。个人 Skills、本地脚本和手工配置的本地 MCP
不受影响；账号侧 Apps、插件和部分产品元数据在 Lean 中不可用。

Browser Full 会把 app-server 产品后端指向固定的 `chatgpt-backend` route，同时把 VS Code 扩展
自身的 `/wham/...` 界面请求切换到 `localhost:8000/api`。后者此前不受 `chatgpt_base_url`
控制，直接连接失败时会让 Codex 侧栏挂载延迟约一分钟；2.4.0 起由独立的受限 loopback 入口
接入同一条浏览器链路。

2.5.0 起，MCP 的可选 GET/`.well-known` 探测由 Host 本地快速结束；日志中的正式
`family=apps-mcp method=POST status=200/204` 才代表协议通信。全局插件目录页面有 10 分钟进程内
缓存、同键并发合并和一次响应头超时重试。普通插件状态查询限 3 并发，建议/精选可使用第 4 个保留槽，
全局目录限 1 并发；这些 GET 都会给
账号初始化及模型/MCP 请求让路。
看到 `request_cache_wait` 后跟 `request_cache_hit family=plugins-list` 表示并发合并生效；缓存不包含
Token、Cookie、安装状态、模型内容或 MCP 工具调用。

2.6.3 起安装器为当前用户启用 Chrome 官方后台模式，计划任务在整个 Windows 登录会话内隐藏监测。
它先用无启动窗口模式初始化扩展；关闭最后一个可见窗口不会结束后台链路，Chrome 被真正终止后连续
两次检查失败会无窗口重启。日志中的 `MONITOR Bridge connection was lost` 后应出现 `STARTING` 和
`MONITOR ready`，不会再以普通空白页兜底。

检查两个本地入口及 VS Code 隐藏设置：

```powershell
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 18888,8000 -ErrorAction SilentlyContinue
(Get-Content "$env:APPDATA\Code\User\settings.json" -Raw | ConvertFrom-Json).'chatgpt.apiEndpoint'
Invoke-WebRequest http://localhost:8000/api/wham/accounts/check -Proxy $null -UseBasicParsing
```

Browser 模式下应显示 `localhost`，账号检查应返回 HTTP 200。Direct 模式会恢复切换前的设置。
如果 8000 被其他程序占用，Host 会在日志写入 `vscode_product_api_unavailable`，模式启动器也会
在打开 VS Code 前停止并给出明确错误；先释放该本地端口再重启 Chrome/Host。

Native Host 默认只记录不含 URL、query、正文和认证信息的分段耗时。先复现一条慢消息，再查看：

```powershell
Get-Content "$env:LOCALAPPDATA\FanVPNBridge\fanvpn-bridge.log" -Tail 500 |
  Select-String 'request_(complete|failed) route=(chatgpt-codex|auth-openai)'
```

`response_head_ms` 表示浏览器取得上游 HTTP 响应头的时间，`first_body_ms` 表示收到第一段
响应数据的时间，`total_ms` 表示流结束时间。这样可以区分代理/TLS 建连慢、认证刷新慢和模型首段
输出慢；日志不会包含 Token 或提示词。

### Browser Full 产品后端诊断

先切换 Full 并启用诊断，再完全退出 Chrome 和 VS Code 后重开：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\set_codex_network_mode.ps1 -Mode BrowserFull
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\set_product_diagnostics.ps1 -Mode Full
```

复现一次问题后查看同一 `request_id` 对应的请求、结果与失败响应摘要：

```powershell
Get-Content "$env:LOCALAPPDATA\FanVPNBridge\fanvpn-bridge.log" -Tail 1000 |
  Select-String 'request_diagnostic|request_complete|request_failed|response_diagnostic'
```

Full 会在本机日志记录完整 URL、非敏感请求头值，以及 HTTP 4xx/5xx 响应的前 4 KiB。Token、Cookie、
API Key、Authorization 和账号 ID 始终自动遮盖；模型请求正文和用户输入不会记录。URL/query 与失败
响应仍可能包含私人标识，收集完成后关闭诊断：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\set_product_diagnostics.ps1 -Mode Off
```

关闭后完全重开 Chrome，使 Native Host 重新读取设置。需要较低风险的路径统计时可用 `-Mode Safe`：
它只保留路径、query 参数名称和 header 名称，query 值会被遮盖。

## VS Code 直连模式无法启动

先确认已关闭全部 VS Code 窗口，再检查可选配置和端口：

```powershell
Test-Path "$env:LOCALAPPDATA\FanVPNBridge\direct-proxy.json"
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 18889 -ErrorAction SilentlyContinue
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\diagnose.ps1
```

点击“Direct US Proxy”后 `direct_mode_configured` 和 `direct_proxy_running` 应为 `true`。
如果配置不存在，重新运行 `install_vscode_direct_mode.ps1`；如果端口被其他程序占用，先确认
占用进程再处理，不要结束未知进程。直连失败不会自动改走本机公网。

如果 VS Code 已打开，启动器会主动拒绝切换。这不是故障：VS Code 后开的窗口会继承首个
实例的环境，必须完全退出后才能可靠选择另一模式。

开发问题的根因和修复背景见[问题与解决记录](PROBLEM_SOLVING.md)。
