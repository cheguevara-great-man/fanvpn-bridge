# Antigravity CLI 浏览器链路

本功能让官方 Antigravity CLI 的 Cloud Code 请求沿用 Browser AI Bridge：

```text
VS Code 集成终端中的 agy.exe
  -> CLOUD_CODE_URL=http://127.0.0.1:18888/antigravity
  -> browser-ai-bridge.exe
  -> Chrome Native Messaging
  -> FanVPN AI Bridge 扩展
  -> Chrome 当前代理扩展
  -> daily-cloudcode-pa.googleapis.com
```

它不会启动 `18889`，不会设置 `HTTP_PROXY`、`HTTPS_PROXY` 或 Windows 系统代理，也不会让 CLI
直接连接 Browser Gateway 服务器。`CLOUD_CODE_URL` 只在启动脚本创建的 CLI 进程中临时存在，CLI
退出后当前 PowerShell 的原值会恢复。

## 准备条件

1. Chrome 中已启用提供境外出口的代理扩展。
2. FanVPN AI Bridge 扩展显示 Native Host 已连接、协议握手完成。
3. 已用当前代码更新 Native Host；`/routes` 中应包含：

```text
antigravity
antigravity-manifest
antigravity-download
```

检查命令：

```powershell
(Invoke-RestMethod http://127.0.0.1:18888/routes -Proxy $null).routes
```

## 通过浏览器链路安装官方 CLI

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\install_antigravity_cli.ps1
```

脚本通过 Chrome 读取 Google 官方发布清单并下载 Windows 程序，随后使用清单中的 SHA-512 校验，
默认安装到：

```text
%LOCALAPPDATA%\agy\bin\agy.exe
```

它不运行第三方安装器，也不会把代理凭据写入磁盘。再次执行可按官方最新清单覆盖更新 CLI；运行中的
`agy.exe` 会锁定文件，更新前应先退出 CLI。

## 在 VS Code 中启动

在 VS Code 的 PowerShell 终端进入要处理的代码目录，然后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  D:\你的路径\fanvpn-bridge\tools\start_antigravity_cli.ps1 `
  -WorkingDirectory (Get-Location).Path
```

首次运行按官方界面登录 Google 账号。登录网页本身由 Chrome 打开，因此也使用 Chrome 中当前启用的
代理扩展。以后仍应从这个启动脚本进入 CLI；直接执行 `agy` 不会带上浏览器 Bridge 的端点设置。

也可以把 CLI 参数放在脚本参数之后，例如：

```powershell
& 'D:\你的路径\fanvpn-bridge\tools\start_antigravity_cli.ps1' `
  -WorkingDirectory (Get-Location).Path `
  -AgyArguments @('--model', 'pro')
```

## 能力边界

- 模型、资格检查及主要账号服务使用 `daily-cloudcode-pa.googleapis.com`，由 `antigravity` 固定路由交给
  Chrome；请求不能指定任意上游，因此 Bridge 仍不是开放代理。
- 安装和手工更新由另外两个固定 Google 源路由完成。
- CLI 自己触发的第三方插件下载、Git、终端命令和任意网页访问不会自动经过 Bridge。它们不是 Cloud
  Code API，请继续使用各自可用的网络方式。
- Google 后续如果删除 `CLOUD_CODE_URL` 官方程序入口或更换服务域名，启动脚本会失效，需要根据新版
  官方 CLI 调整固定路由；脚本不会在失败时偷偷回退到直连。

## 排查

启动脚本提示缺少 `antigravity` 路由时，说明代码已经替换但 Native Host 还是旧构建。运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\update_native_host.ps1
```

然后刷新 FanVPN AI Bridge 扩展并重开 Chrome。若登录或请求仍失败，查看：

```powershell
Get-Content "$env:LOCALAPPDATA\FanVPNBridge\fanvpn-bridge.log" -Tail 200 |
  Select-String 'route=antigravity|request_failed|browser_fetch_failed'
```

日志中出现 `route=antigravity` 才能证明该请求确实进入了浏览器链路。
