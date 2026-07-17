# Windows 安装

本文只介绍如何在 Windows 上安装 FanVPN Bridge。安装完成后，再按[客户端使用指南](USAGE.md)配置
Codex、Claude Code 或 CC Switch。

## 系统要求

- Windows 10 或 11。
- Google Chrome 116+。
- Chrome 中已安装并启用 FanVPN。
- Python 3.12+，仅在构建 Native Host 时需要。

项目当前只支持 Google Chrome，不注册 Microsoft Edge。

## 1. 获取源码

使用 Git：

```powershell
git clone https://github.com/cheguevara-great-man/fanvpn-bridge.git
Set-Location .\fanvpn-bridge
```

如果命令行无法访问 GitHub，也可以在 Chrome 中下载仓库 ZIP，解压后在 PowerShell 中进入解压目录。
后续命令都应在仓库根目录执行。

## 2. 构建 Native Host

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1
```

脚本会验证 `python` 确实是 Python 3.12+，按固定版本安装并缓存 PyInstaller，然后将产物写入
`dist\browser-ai-bridge\`。成功时最后一行类似：

```text
Native Host built at: <仓库路径>\dist\browser-ai-bridge
```

PyInstaller 工具、work 和 spec 缓存默认写入：

```text
%LOCALAPPDATA%\BrowserAIBridge\build-cache
```

它们不再放在源码仓库的 `build` 目录中，避免 VS Code 文件监视、搜索和语言服务扫描大批第三方
构建文件。工具缓存按 PyInstaller 版本复用，work/spec 再按仓库绝对路径的摘要隔离；最终可部署
产物仍写入 `dist`、`dist-a` 或 `dist-b`。如果旧版本曾在仓库内生成 `build\pyinstaller*`，该目录
已经不参与新构建，可以在确认没有构建进程运行后删除。

如果系统中有多个 Python，可以显式指定解释器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 `
  -Python "C:\path\to\python.exe"
```

需要把构建缓存放到其他磁盘时，可显式指定：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 `
  -BuildCacheRoot 'D:\BrowserAIBridgeBuildCache'
```

## 3. 加载 Chrome 扩展

1. 在 Chrome 打开 `chrome://extensions`。
2. 开启右上角的“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择仓库中的 `chrome-extension` 目录。
5. 打开 **FanVPN AI Bridge → 详情**，将“网站访问权限”设为“在所有网站上”。

扩展 ID 应为 `bgpbajocpomglgdffkgcklhepbcfpbfd`。Manifest 中用于固定该 ID 的 key 是公开的
扩展标识，不是 API Key，也不包含私钥。

## 4. 注册 Native Host

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

安装脚本会：

- 为 Chrome 注册 `browser-ai-bridge.exe`。
- 生成 Native Messaging manifest。
- 将 `127.0.0.1,localhost` 合并进当前用户的 `NO_PROXY`。
- 注册 `FanVPN Bridge Bootstrap` Windows 登录任务。

确认命令输出中的 `Native Host` 和 `Manifest` 都位于当前仓库的
`dist\browser-ai-bridge\`。然后回到 `chrome://extensions`，点击 FanVPN AI Bridge 的刷新按钮。

安装脚本的可选参数：

- `-SkipNoProxy`：不修改用户级 `NO_PROXY`。
- `-SkipStartupTask`：不注册 Windows 登录任务。

## 5. 验证安装

打开 FanVPN AI Bridge 扩展弹窗，正常状态应为：

- Native Host：已连接。
- 协议握手：完成。
- 浏览器执行器：`offscreen`。
- ChatGPT 网站权限：已授权。
- 弹窗下方出现“启动 VS Code 网络模式”，并提供“服务器直连”“浏览器精简”和
  “浏览器完整（实验）”三个按钮。

第一次选择前，弹窗可能显示“未由 Bridge 管理”；这只表示现有 Codex provider 还不是 2.6.0 的
托管配置，不代表 Native Messaging 故障。三模式按钮要求 Chrome 扩展和正在运行的 Native Host
均为 2.6.0；只刷新扩展但仍运行旧 Host 时，模式读取或切换会失败。

在 PowerShell 中运行：

```powershell
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/routes -Proxy $null
```

`/ready` 应返回 `ready = true`。`/routes` 应包含：

```text
anthropic
auth-openai
chatgpt-codex
chatgpt-backend
gemini
gemini-openai
openai
```

`chatgpt-backend` 的存在不代表默认会使用它：Browser Lean 不设置产品后端地址，只有显式选择
Browser Full 才会发起这类请求。如果该路由缺失，说明运行中的 Host 不是当前版本，请核对安装输出中的
Native Host 路径并完全重开 Chrome。

如需完整诊断，可运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\diagnose.ps1
```

安装脚本修改的用户环境变量只会被新启动的程序读取，因此此时应完全退出并重新打开 VS Code。
接下来按[客户端使用指南](USAGE.md)配置所需客户端。

## 日常启动

Windows 用户登录时，`FanVPN Bridge Bootstrap` 会在后台启动 Chrome，并等待扩展拉起 Native Host。
它不会替用户开启 FanVPN、选择节点或授予扩展权限；这些仍由当前 Chrome 配置文件中的扩展管理。
安装器会为当前用户启用 Chrome 官方后台模式，登录任务则在整个登录会话内隐藏运行并监测 Bridge。
因此关闭所有可见 Chrome 窗口后，VS Code 浏览器链路仍可继续；如果 Chrome 被真正结束或更新退出，
看护任务会在约 30 秒内无窗口恢复，不会弹出空白 Chrome 窗口。

如果 Chrome 已经打开，Bridge 通常由扩展自动连接。也可以手动触发登录任务：

```powershell
Start-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap'
```

## 更新

项目使用 `dist-a` 和 `dist-b` 两套构建目录交替更新。更新脚本会读取 Chrome 当前注册的 Host，
自动构建到另一套目录，先启动新 EXE 完成冒烟测试，再切换注册；首次从默认的 `dist` 更新时选择
`dist-a`。构建、测试或安装失败时不会留下指向坏产物的注册。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\update_native_host.ps1
```

脚本完成后：

1. 在 `chrome://extensions` 刷新 FanVPN AI Bridge。
2. 扩展刷新会主动断开旧 Host，并按新注册路径启动另一槽位；无需关闭全部 Chrome。
3. 等待数秒后重新检查 `/ready` 和 `/routes`。

2.6.0 的三模式弹窗依赖打包进 Native Host 目录的固定启动脚本，因此更新时不能只替换
`chrome-extension`。必须先运行更新脚本切换 Host，再刷新扩展，确认弹窗和
`/ready` 报告的版本一致。

下一次更新会自动使用刚刚释放的另一套目录。可以用 `-WhatIf` 只查看本次将使用哪一套，而不执行构建和注册：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\update_native_host.ps1 -WhatIf
```

如果新槽位在重开 Chrome 后出现问题，可以切回另一套已经存在的 A/B 产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\update_native_host.ps1 -Rollback
```

回滚同样会先冒烟测试目标 EXE。当前注册尚未进入 `dist-a` / `dist-b` 时不能使用 `-Rollback`。

更新脚本会在构建前自动结束**非活动目标槽**中残留的旧 Host，不影响当前注册槽。如果仍报告
`WinError 5`，通常是杀毒软件或文件索引器正在扫描目标 DLL；等待数秒后重试，必要时暂时关闭 Chrome。

如果已经安装可选的 VS Code 直连模式，更新前还要关闭全部 VS Code 窗口并点击一次
“VS Code - Browser Bridge”。更新脚本检测到 `18889` 直连进程时会拒绝继续并给出提示，避免覆盖
正在运行的 A/B 槽位。

## 日志

```text
%LOCALAPPDATA%\FanVPNBridge\startup.log
%LOCALAPPDATA%\FanVPNBridge\fanvpn-bridge.log
```

运行日志按 2 MiB 轮转并保留三个历史文件。常规日志不记录请求正文、Token、Cookie、API Key 或
认证头值。开启 Full 产品诊断时可以记录非敏感 header 值和失败响应摘要，但认证凭据始终遮盖。

经 Chrome 执行的成功请求会在 `request_complete` 记录 `executor_queue_ms`、`fetch_head_ms`、`fetch_attempts` 和
`fetch_preemptions`；浏览器在返回响应头前失败时，会先记录带相同字段的 `browser_fetch_failed`，
随后记录 `request_failed`。本地快速响应和内存缓存命中没有 browser fetch，因此不会带这组时序。
这些时序只用于区分浏览器排队、实际 fetch 和抢占/重试，不包含 URL、正文或凭据。

## 可选：安装 VS Code 直连模式

只有已经部署配套 Browser Gateway HTTPS 代理，并希望 VS Code 可绕过 Chrome 直接使用它时，
才需要执行本节。普通浏览器桥接用户可以跳过。

部署服务器的电脑默认已有：

```text
C:\Users\<Windows 用户名>\.browser-gateway\deployment.local.json
```

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\install_vscode_direct_mode.ps1
```

如果凭据文件来自另一台电脑，先用安全的离线方式把它放到本机，再明确指定路径：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\install_vscode_direct_mode.ps1 `
  -CredentialPath 'D:\安全位置\deployment.local.json'
```

脚本会校验并复制凭据到 `%LOCALAPPDATA%\FanVPNBridge\direct-proxy.json`，限制为当前
Windows 用户可读，并在桌面建立“VS Code - Browser Bridge”、
“VS Code - Browser Full (Experimental)”和“VS Code - Direct US Proxy”三个入口。
扩展弹窗中的两个浏览器模式不要求执行本节；只有“服务器直连”需要该凭据文件。安装脚本不会
设置 Windows 全局代理，也不会自动启用直连模式。

## 卸载

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

然后在 `chrome://extensions` 中移除 FanVPN AI Bridge。卸载不会删除 FanVPN、仓库、构建目录、
Codex/Claude 数据或日志。卸载登录任务时会恢复安装前的 Chrome 后台模式策略。
