# Codex 服务器中心 API

本文说明 `codex/server-executor` 分支已经实现的 **Server Lite** 功能、部署方式和日常使用方法。
开发过程和第二阶段设计保留在[总体方案](SERVER_CODEX_EXECUTOR_PLAN.md)；普通使用者只需阅读本文。

## 1. 它解决什么问题

服务器中心模式把 ChatGPT Codex 账号保存在美国服务器，由服务器替各台已注册电脑向 Codex 模型接口发出请求。
客户端电脑只保存自己的设备 Token，不保存服务器账号的 access token、refresh token 或 Cookie。

```text
VS Code Codex / Codex CLI
  → http://127.0.0.1:18890/v1
  → 本机 Server Client
  → 旧浏览器链路：127.0.0.1:18888/server-executor
  → Chrome + Browser Gateway
  → https://服务器:9444/v1/codex
  → 服务器 Codex Executor
  → ChatGPT Codex
```

服务器只承担账号认证、模型请求、用量统计和设备额度控制。文件读取、代码修改、终端、Git、Skills、
本地 MCP 和子 Agent 仍由每台电脑上的 Codex 执行，因此各电脑的工作区不会被上传到另一台电脑执行。

## 2. 当前已实现的能力

- 服务器集中保存一个 ChatGPT Codex 登录账号并自动刷新凭据；
- 按设备 Token 验证请求，停用设备后拒绝该设备继续调用；
- 支持模型目录、Responses 流式请求、压缩、查询、取消和删除；
- 服务器权威记录 Token/Credits，并复用现有设备额度策略；
- 本机 `18890` 只监听 `127.0.0.1`，不是局域网服务或通用代理；
- 默认仍通过 Chrome 和 Browser Gateway 到达服务器，公司电脑不需要 Clash；
- 与旧 `18888` 浏览器链路并行，可随时切回；
- Windows、Chrome 或 Native Host 重启后，自动恢复已经选中的 `18890` 客户端；
- 启动失败时停止残留进程、恢复原配置并回退旧浏览器链路。

当前 **尚未实现 Server Full**。因此服务器中心模式暂不承诺 ChatGPT 云端历史、Apps、远程插件目录、
账号 MCP 和其他产品接口。个人 Skills、本地脚本、本地 MCP、终端和 Git 不依赖 Server Full，仍可正常使用。

## 3. 端口和组件

| 位置 | 端口/路径 | 用途 |
|---|---|---|
| Windows | `127.0.0.1:18888` | 既有 Native Host 与 Chrome 浏览器链路 |
| Windows | `127.0.0.1:18890` | 独立 Server Client，给 Codex 提供 OpenAI Responses 兼容入口 |
| 服务器 | TCP `9444` | 设备认证后的 Codex Executor HTTPS 入口 |
| 服务器 | TCP `9443` | 既有设备注册、用量统计和管理网页 |

`18888` 与 `18890` 不冲突：前者负责浏览器执行，后者只服务服务器中心 Provider。选择服务器中心（经浏览器）时，
`18890` 会把固定请求交给 `18888/server-executor`，不能借此访问任意网站。

## 4. 准备条件

服务器端需要：

1. Browser Gateway 已正常部署，设备注册和统计网页可以使用；
2. `browser-gateway` 仓库切到 `codex/server-executor`；
3. 服务器 `9444/TCP` 已在防火墙和云安全组放行；
4. 准备用作服务器中心的 ChatGPT Codex 账号符合相应套餐和使用规则。

Windows 电脑需要：

1. `fanvpn-bridge` 仓库切到 `codex/server-executor`；
2. FanVPN AI Bridge 3.8.5 或更高版本；
3. Browser Gateway 和 FanVPN AI Bridge 两个 Chrome 扩展均已加载；
4. Native Host、Chrome 浏览器链路和 Browser Gateway 代理均正常。

> 此功能仍在开发分支。插件内“一键更新”固定跟随 `master`，测试本分支期间不要用它覆盖源码；应在分支目录中
> `git pull` 后运行 A/B Native Host 更新脚本。

## 5. 首次部署服务器

以下命令在 **Browser Gateway 项目目录的 PowerShell** 中运行。把占位符替换成服务器地址；不要把密码、
设备 Token 或 `auth.json` 提交到 Git。

### 5.1 安装独立执行器

```powershell
git switch codex/server-executor
git pull

powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\deploy-codex-executor.ps1 `
  -Server '<服务器IP>' `
  -IdentityFile "$env:USERPROFILE\.ssh\browser_gateway_ed25519"
```

该脚本安装独立的 `browser-gateway-codex-executor.service` 和 TCP `9444`，不会替换既有 Gateway、
GOST、sing-box、用量网页或 `9443` 服务。

### 5.2 上传服务器账号

先在执行这一步的电脑上让 Codex 登录准备使用的 ChatGPT 账号，确认
`$env:USERPROFILE\.codex\auth.json` 存在，然后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\set-server-codex-account.ps1 `
  -Server '<服务器IP>' `
  -IdentityFile "$env:USERPROFILE\.ssh\browser_gateway_ed25519"
```

脚本只把凭据安装到服务器的受限目录，不打印 Token，也不写入仓库。以后账号主动退出、修改密码或 refresh token
被撤销时，需要重新登录并再次执行这一步。普通使用电脑不需要复制这份 `auth.json`。

### 5.3 验证服务器

```powershell
Test-NetConnection '<服务器IP>' -Port 9444

ssh -i "$env:USERPROFILE\.ssh\browser_gateway_ed25519" root@'<服务器IP>' `
  'systemctl is-active browser-gateway-codex-executor'
```

应分别看到 `TcpTestSucceeded : True` 和 `active`。未携带设备 Token 直接访问 `9444` 返回 `401` 属于正常安全行为。

## 6. 在一台 Windows 电脑启用

### 6.1 更新分支和 Native Host

在 `fanvpn-bridge` 分支目录运行：

```powershell
git switch codex/server-executor
git pull

powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\update_native_host.ps1 `
  -Python (Get-Command python).Source
```

然后在 `chrome://extensions` 中刷新 FanVPN AI Bridge。若 Chrome 仍占用旧 A/B 槽，完全退出并重新打开 Chrome。

### 6.2 注册设备

1. 管理员登录服务器的 `/dashboard`，进入“设备额度”；
2. 填写设备名称并生成十分钟有效的一次性注册码；
3. 在目标电脑打开 Browser Gateway 插件，输入注册地址和注册码；
4. 点击“注册这台设备”；若以前已注册，则点击“重新同步到 AI Bridge”。

FanVPN AI Bridge 3.8.5 起会同时生成：

```text
%LOCALAPPDATA%\FanVPNBridge\usage-reporting.json
%LOCALAPPDATA%\FanVPNBridge\server-executor.json
```

前者用于统计，后者保存服务器地址和该设备自己的设备 Token。正常使用不需要手工打开或复制这两个文件。

### 6.3 选择服务器中心

建议先完全退出所有 VS Code 窗口，然后：

1. 打开 FanVPN AI Bridge 插件；
2. 在“Codex 链路选择”中点击“服务器中心（经浏览器）”；
3. 等待提示链路切换成功；
4. 重新打开 VS Code，再进入 Codex。

按钮会启动独立的 `browser-ai-bridge.exe --server-client`，监听 `18890`，并把 Codex Provider 改为：

```toml
model_provider = "server_codex_executor"

[model_providers.server_codex_executor]
base_url = "http://127.0.0.1:18890/v1"
requires_openai_auth = false
wire_api = "responses"
supports_websockets = false
```

这段配置由插件管理，不需要手工输入。服务器中心模式不使用本机 OpenAI API Key，也不要求普通电脑持有服务器账号。

## 7. 验证本机链路

先检查旧浏览器链路：

```powershell
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
```

应看到 `ready=True`、`native_channel_connected=True`，并且 `routes` 包含 `server-executor`。

选择服务器中心后检查独立客户端：

```powershell
Invoke-RestMethod http://127.0.0.1:18890/ready -Proxy $null
```

应返回：

```json
{"status":"ok","ready":true,"mode":"server-client"}
```

再检查 Codex 配置：

```powershell
Select-String "$env:USERPROFILE\.codex\config.toml" -Pattern `
  'model_provider|server_codex_executor|127\.0\.0\.1:18890'
```

最后在 VS Code Codex 新建任务并发送一条消息。服务器统计网页应把请求计入当前注册设备。

## 8. 切回旧浏览器链路

完全退出 VS Code，在 FanVPN AI Bridge 的“Codex 链路选择”中点击“旧浏览器链路”，再重新打开 VS Code。
Bridge 会停止独立 `18890` 进程，并恢复切换前的 Provider；`18888`、Chrome 和 Browser Gateway 不会被关闭。

点击上方任意普通 Codex 模式（浏览器精简、浏览器完整、仅 Gemini 或 Hybrid）时，插件也会先退出服务器中心，
避免 `18888` 与 `18890` 两套 Provider 同时被选中。

## 9. 可选 Direct 传输

“服务器中心（经浏览器）”按钮始终优先使用 Chrome 链路。只有在网络和公司政策允许 VS Code 本机进程直接访问
服务器时，才使用 Direct：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\configure_server_executor_client.ps1 `
  -ExecutorUrl 'https://<服务器IP>:9444/v1/codex' `
  -DeviceToken '<该设备Token>' `
  -Transport Direct `
  -Start
```

这是高级配置。普通公司电脑使用默认 Browser 传输即可，不需要手工处理设备 Token。

## 10. 常见问题

### 插件显示“无法读取链路状态”

通常是 Chrome 扩展与 Native Host 版本不一致。3.8.4 起该错误只会禁用服务器中心按钮，不会影响上方普通
Codex 模式。运行 `tools\update_native_host.ps1`，刷新扩展并重启 Chrome。

### 显示“服务器中心尚未配置”

目标电脑还没有 `server-executor.json`。在 Browser Gateway 中完成设备注册，或点击“重新同步到 AI Bridge”。

### `18890/ready` 无法连接

再次点击“服务器中心（经浏览器）”。同时确认 `18888/ready` 正常、Chrome 没有退出、两个扩展均启用。
日志位于：

```text
%LOCALAPPDATA%\FanVPNBridge\fanvpn-bridge.log
```

### 返回 `401`

- 本机到服务器的 `401`：设备 Token 无效、设备已撤销，或错误地使用了全局用量 Token；
- 服务器到 ChatGPT 的 `401`：服务器账号凭据过期或被撤销，重新执行 `set-server-codex-account.ps1`。

### 返回 `429 machine_credit_limit_reached`

这是服务器设备额度策略拦截，不是 OpenAI 网络故障。在管理网页检查当前周期、均分设备数、设备状态和额度。

### 返回 `502 server_unreachable`

检查 Browser Gateway 是否开启、服务器 `9444` 是否可达，以及
`browser-gateway-codex-executor.service` 是否为 `active`。

### Chrome 关闭后无法使用

默认 Browser 传输依赖 Chrome Offscreen 执行器，Chrome 完全退出后链路会停止。这是预期边界。需要脱离 Chrome
时只能使用明确允许的 Direct 传输。

## 11. 安全边界

- 服务器账号只保存在 `/var/lib/browser-gateway/codex-executor`，权限限制给专用服务账号；
- 客户端设备 Token 不写入 Codex TOML，也不显示在插件状态页；
- 服务器数据库只保存设备 Token 哈希；
- 公网入口只允许固定 Codex 路径，不提供 CONNECT、SOCKS 或任意 URL 转发；
- 日志不记录请求正文、用户输入、Cookie、登录 Token 或设备 Token；
- 停用或撤销设备后，该设备不能继续调用服务器中心 API；
- 多台设备应遵守 ChatGPT 账号、套餐、组织和公司网络的适用规则。

