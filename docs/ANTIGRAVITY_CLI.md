# Antigravity CLI 浏览器链路

本功能让官方 Antigravity CLI 的模型请求、登录校验和令牌刷新全部经过 Chrome，不需要让 VS Code、终端或 CLI 直接连接境外服务器。

## 推荐：扩展内一键配置

更新 Native Host 并刷新 FanVPN AI Bridge 扩展后，打开扩展弹窗，在“Antigravity for VS Code”区域点击“一键配置 Antigravity”。按钮会自动完成：

1. 通过 Chrome 下载并校验官方 Antigravity CLI。
2. 生成浏览器链路专用的 `agy-browser.exe`。
3. 通过 Chrome 下载 VS Code 社区扩展，并验证扩展身份后安装。
4. 保留原有 VS Code 设置并写入 `antigravity.cliPath`。
5. 设置用户级 `CLOUD_CODE_URL`。
6. 清理旧版登录兼容标记，并复查扩展确实安装成功。
7. 为社区扩展应用项目内置的 Windows ConPTY 兼容修复。

配置完成后只需完全退出并重开一次 VS Code。CLI 不需要常驻：VS Code 插件会在创建会话时自动启动，在会话结束时退出。按钮显示“已配置”后，平时无需再点；需要检查官方 CLI 更新或修复配置时可以再次点击。

按钮不会安装通用代理、不会修改系统代理，也不会保存 Google 密码。

社区扩展 `lyadhgod.antigravity-vscode` 0.13.2 仍通过旧文件
`~/.gemini/antigravity-cli/antigravity-oauth-token` 判断是否已经登录；Windows 版官方
Antigravity CLI 1.1.5 已改用 Windows 凭据管理器，不再创建该文件。项目内置的 Windows
兼容版扩展会直接检查 `gemini:antigravity` 凭据是否存在（不会读取凭据内容），因此新电脑会正确
显示登录界面，登录完成后也会立即识别。旧版脚本遗留的空标记或兼容标记会被自动清理；如果未来 CLI
创建了真实的非空文件，脚本不会删除它。

该社区扩展的原版交互会话通过 Linux/macOS 的 `script` 命令创建伪终端，在 Windows
上会导致“The Antigravity session ended before it was ready”。一键配置固定安装经过身份
校验的 0.13.2，并只替换其交互进程适配层：Unix 行为保持不变，Windows 改用 VS Code
自带且 ABI 匹配的 `node-pty`/ConPTY。补丁来源、许可证和构建产物位于
`tools/vendor/antigravity-vscode-0.13.2/`。

```text
Antigravity CLI
  -> 127.0.0.1:18888
  -> Browser AI Bridge Native Host
  -> FanVPN AI Bridge Chrome 扩展
  -> Chrome 当前启用的代理扩展
  -> Google 服务
```

它不会启动通用代理端口，不会修改 Windows 系统代理，也不会让 CLI 直接连接 Browser Gateway 服务器。Chrome 必须保持运行；窗口可以最小化，但 Chrome 完全退出后浏览器执行器也会断开。

## 为什么有两个 CLI 文件

安装后会看到：

```text
%LOCALAPPDATA%\agy\bin\agy.exe
%LOCALAPPDATA%\agy\bin\agy-browser.exe
```

- `agy.exe` 是从 Google 官方发布地址下载、并通过官方清单 SHA-512 校验的原文件，始终保留不动。
- `agy-browser.exe` 是专用于浏览器链路的副本。脚本只把程序中两个固定的 Google OAuth 地址替换成本机 Bridge 地址，OAuth 客户端、PKCE、请求内容、账号凭据和模型协议都不改变。

必须使用浏览器副本，是因为 Antigravity CLI 1.1.5 只允许通过 `CLOUD_CODE_URL` 改写模型服务地址，用户信息校验和令牌交换地址仍被写死在程序中。直接运行官方 `agy.exe` 时，可能看到：

```text
token exchange failed: Post "https://oauth2.googleapis.com/token"
```

这表示它正在绕过浏览器直连 Google，并非账号密码错误。

## 准备条件

1. Chrome 已启用可访问境外网络的代理扩展。
2. FanVPN AI Bridge 扩展显示 Native Host 已连接、协议握手完成。
3. 当前 Native Host 的路由包含：

```text
antigravity
agi
google
antigravity-avatar
antigravity-manifest
antigravity-download
vscode-marketplace
```

检查命令：

```powershell
(Invoke-RestMethod http://127.0.0.1:18888/routes -Proxy $null).routes
```

其中 `agi` 和 `google` 是 Bridge 内部使用的短路由名。它们之所以较短，是为了让替换后的地址与官方程序中的原地址字节数完全相同，避免破坏可执行文件结构。`antigravity-avatar` 用于读取资格检查所需的 Google 账号头像。

## 安装或更新

在仓库根目录的 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\install_antigravity_cli.ps1
```

脚本会依次完成：

1. 通过 Chrome 获取 Google 官方发布清单和 Windows CLI。
2. 使用官方清单中的 SHA-512 校验下载文件。
3. 保存未经修改的 `agy.exe`。
4. 自动生成 `agy-browser.exe`。

如果 CLI 正在运行并锁定文件，请先退出后再更新。

## 启动

进入希望 CLI 操作的项目目录，在 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  D:\你的路径\fanvpn-bridge\tools\start_antigravity_cli.ps1 `
  -WorkingDirectory (Get-Location).Path
```

启动脚本会：

- 检查 Chrome 和 Bridge 是否就绪；
- 检查运行所需的四条固定路由；
- 始终运行 `agy-browser.exe`，不会在失败时退回会直连的 `agy.exe`；
- 官方文件更新后自动重新生成浏览器副本；
- 仅给本次 CLI 进程设置模型服务地址，并临时清除继承的通用代理环境变量；
- CLI 退出后恢复当前 PowerShell 原有环境。

首次运行按官方界面登录 Google 账号。授权网页由 Chrome 打开；授权码换令牌、后续刷新令牌及模型请求也都通过浏览器链路完成。

不要直接运行 `agy` 或 `agy.exe`，否则 OAuth 请求仍会直接访问 Google。

## 验证

不依赖 Clash 或系统代理时，可以运行：

```powershell
& '.\tools\start_antigravity_cli.ps1' `
  -WorkingDirectory (Get-Location).Path `
  -AgyArguments @('--print', 'Reply with OK only')
```

成功时会输出 `OK`。Bridge 日志中应同时出现模型路由以及在需要时出现的登录路由：

```powershell
Get-Content "$env:LOCALAPPDATA\FanVPNBridge\fanvpn-bridge.log" -Tail 300 |
  Select-String 'route=(antigravity|agi|google|antigravity-avatar)'
```

## 能力边界

- Google Cloud Code 模型服务、OAuth 用户信息校验、令牌交换、刷新和资格检查所需的账号头像均走浏览器链路。
- Git、终端命令、任意网页和 CLI 自己下载的第三方工具不会自动经过 Bridge，因为 Bridge 不是开放式通用代理。
- Google 若在新版 CLI 中更换内置 OAuth 地址，生成浏览器副本时会明确报“不支持的版本”，不会静默退回直连。

## 常见问题

### 仍然出现 `oauth2.googleapis.com/token` 直连错误

说明启动了官方 `agy.exe`，或者仍在使用更新前已经打开的 CLI 进程。退出旧进程，并使用 `tools\start_antigravity_cli.ps1` 重新启动。

### 提示缺少路由

更新 Native Host：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\update_native_host.ps1
```

然后刷新 FanVPN AI Bridge 扩展并重开 Chrome。

### `Eligibility Check` 返回 403

先确认 Chrome 的代理扩展确实开启，再刷新 FanVPN AI Bridge 扩展。最新版扩展会为 Cloud Code 请求保留 CLI 的原始 User-Agent，并移除 Chrome 自动添加、会触发 Google 拒绝的 Origin 和 Referer。

### `The Antigravity session ended before it was ready`

先再次执行“一键配置 Antigravity”，然后完全退出并重开 VS Code。新版配置会自动安装
Windows ConPTY 兼容构建；不需要单独安装 Unix 工具，也不需要自己修改 VS Code 文件。
