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

脚本会自动使用 `python` 命令对应的解释器，安装构建依赖，并将产物写入
`dist\browser-ai-bridge\`。成功时最后一行类似：

```text
Native Host built at: <仓库路径>\dist\browser-ai-bridge
```

如果系统中有多个 Python，可以显式指定解释器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 `
  -Python "C:\path\to\python.exe"
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
gemini
gemini-openai
openai
```

如需完整诊断，可运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\diagnose.ps1
```

安装脚本修改的用户环境变量只会被新启动的程序读取，因此此时应完全退出并重新打开 VS Code。
接下来按[客户端使用指南](USAGE.md)配置所需客户端。

## 日常启动

Windows 用户登录时，`FanVPN Bridge Bootstrap` 会在后台启动 Chrome，并等待扩展拉起 Native Host。
它不会替用户开启 FanVPN、选择节点或授予扩展权限；这些仍由当前 Chrome 配置文件中的扩展管理。

如果 Chrome 已经打开，Bridge 通常由扩展自动连接。也可以手动触发登录任务：

```powershell
Start-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap'
```

## 更新

先构建到新的输出目录，避免正在运行的 Host 锁定旧目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 `
  -DistRoot .\dist-next
```

再将 Chrome 注册切换到对应产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -BuildDirectory .\dist-next\browser-ai-bridge
```

必须让 `-BuildDirectory` 与实际构建目录匹配；不带该参数的 `install.ps1` 始终使用默认的
`dist\browser-ai-bridge`。切换后刷新扩展、重开 Chrome，并重新检查 `/ready` 和 `/routes`。

如果构建报 `WinError 5` 或 DLL 拒绝访问，说明目标目录仍被 Host 使用。关闭 Chrome 并等待
`browser-ai-bridge.exe` 退出，或者换一个新的 `-DistRoot`。

## 日志

```text
%LOCALAPPDATA%\FanVPNBridge\startup.log
%LOCALAPPDATA%\FanVPNBridge\fanvpn-bridge.log
```

运行日志按 2 MiB 轮转并保留三个历史文件。日志不记录请求正文、Token、Cookie 或请求头值。

## 卸载

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

然后在 `chrome://extensions` 中移除 FanVPN AI Bridge。卸载不会删除 FanVPN、仓库、构建目录、
Codex/Claude 数据或日志。
