# Windows 安装

## 前置条件

- Windows 10 或 11。
- Google Chrome 116+。
- Chrome 中已安装 FanVPN，并能够在普通标签页访问目标网站。
- 构建需要 Python 3.12+；运行打包产物不需要系统 Python。
- 开发和检查脚本需要 Node.js 22+。

项目只注册 Google Chrome，不写入 Edge 的 Native Messaging 注册表。

## 新电脑完整部署（已验证）

以下流程适用于没有 Clash、仅通过 Chrome 中 FanVPN 出口联网的 Windows 电脑。PowerShell 中切换目录直接使用
`Set-Location` 或 `cd`，不要使用 CMD 专用的 `cd /d`。

```powershell
Set-Location 'D:\software\AI_Coding\codex\browser-ai-bridge'
git status --short
python --version
```

如果 Git 无法访问 GitHub，可以在能够访问 GitHub 的 Chrome 中下载仓库 ZIP，再解压到目标目录；不要把路径或命令先经过
Markdown 转换工具，否则 `_`、`*`、`[` 等字符可能被自动加上反斜杠。

依次构建、加载扩展并注册 Host：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

然后在 `chrome://extensions` 加载仓库的 `chrome-extension` 目录，将网站访问权限设为“在所有网站上”，刷新扩展并确认弹窗显示：

- Native Host：已连接
- 协议握手：完成
- 浏览器执行器：`offscreen`
- ChatGPT 网站权限：已授权

最后验证：

```powershell
curl.exe http://127.0.0.1:18888/ready
curl.exe http://127.0.0.1:18888/routes
```

`/ready` 应包含 `"ready":true`，`/routes` 应至少包含 `anthropic`、`auth-openai`、`chatgpt-codex`、
`gemini`、`gemini-openai` 和 `openai`。这只证明 Bridge 链路就绪；FanVPN 节点仍需用户在 Chrome 中开启和切换。

## 构建 Native Host

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 `
  -Python "C:\path\to\python.exe"
```

输出位于 `dist\browser-ai-bridge\`，包含 `browser-ai-bridge.exe`、运行库和
`routes.json`。

如果现有进程锁定默认输出目录，可构建到新目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 `
  -Python "C:\path\to\python.exe" -DistRoot .\dist-next
```

构建到自定义目录后，安装时必须明确指定同一产物目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -BuildDirectory .\dist-next\browser-ai-bridge
```

单独运行 `install.ps1` 始终注册默认的 `dist\browser-ai-bridge`，不会自动选择最近生成的 `dist-next`。

## 加载 Chrome 扩展

1. 打开 `chrome://extensions`。
2. 开启“开发者模式”。
3. 选择“加载已解压的扩展程序”。
4. 选择仓库中的 `chrome-extension` 目录。
5. 确认扩展 ID 为 `bgpbajocpomglgdffkgcklhepbcfpbfd`。
6. 在 **FanVPN AI Bridge → 详情** 中，把“网站访问权限”设为“在所有网站上”。

Manifest 中的公开开发 identity key 用来稳定扩展 ID，不是 API Key，也不包含私钥。

## 注册 Native Host

默认构建目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

自定义构建目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -BuildDirectory .\dist-next\browser-ai-bridge
```

安装脚本会：

- 生成 Native Messaging manifest。
- 写入 `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge`。
- 将 `127.0.0.1,localhost` 合并进用户级 `NO_PROXY`。
- 注册当前用户的 `FanVPN Bridge Bootstrap` 登录任务。

可选参数：

- `-SkipNoProxy`：不修改用户级 `NO_PROXY`。
- `-SkipStartupTask`：不注册登录任务。

安装后刷新 FanVPN AI Bridge 扩展，并重启 VS Code，使新的用户环境变量生效。

## 验证安装

扩展弹窗应显示 Native Host 已连接、协议握手完成、浏览器执行器为 `offscreen`。

```powershell
Invoke-RestMethod http://127.0.0.1:18888/health -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/routes -Proxy $null
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\diagnose.ps1
```

## 自动启动

`FanVPN Bridge Bootstrap` 在 Windows 用户登录时启动 Chrome，并最多等待三分钟，
直到扩展拉起 Native Host 且 `/ready` 成功。失败时任务计划最多重试五次。

它不会自动打开 FanVPN 节点，也不能替用户授予扩展网站权限。FanVPN 扩展仍需在
当前 Chrome profile 中保持启用。

手动触发：

```powershell
Start-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap'
```

## 更新

1. 构建到新的输出目录。
2. 用 `install.ps1 -BuildDirectory ...` 将注册表切换到新构建。
3. 在 `chrome://extensions` 刷新 FanVPN AI Bridge。
4. 关闭并重新打开 Chrome，使扩展拉起新 Host。
5. 检查 `/ready` 和 `diagnose.ps1`。

不要在运行中的 Host 仍锁定目录时覆盖其文件。

## 日志

```text
%LOCALAPPDATA%\FanVPNBridge\startup.log
%LOCALAPPDATA%\FanVPNBridge\fanvpn-bridge.log
```

运行日志按 2 MiB 轮转并保留三个历史文件。日志不记录请求正文、Token、Cookie
或请求头值。

## 卸载

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

然后从 Chrome 移除 FanVPN AI Bridge。卸载不会删除 FanVPN、仓库、构建目录、
Codex/Claude 数据或日志。
