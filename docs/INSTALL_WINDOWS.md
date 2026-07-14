# Windows 安装与诊断

## 前置条件

- Google Chrome 116+
- Chrome 中已安装并连接 FanVPN
- 开发构建需要 Python 3.12+；运行构建产物不需要 Python

本项目只注册 Google Chrome 的 Native Messaging 路径，不写入 Edge 注册表。

## 1. 构建 Native Host

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 -Python "C:\path\to\python.exe"
```

输出目录：`dist\fanvpn-bridge\`。其中包含 `fanvpn-bridge.exe`、Python 运行库和 `routes.json`。

若已有版本正在运行并锁定目录，可构建到新的版本目录后重新注册：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 `
  -Python "C:\path\to\python.exe" -DistRoot .\dist-next
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -BuildDirectory .\dist-next\fanvpn-bridge
```

## 2. 加载 Chrome 扩展

1. 打开 `chrome://extensions`。
2. 开启“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择仓库中的 `chrome-extension` 目录。
5. 确认 ID 为 `bgpbajocpomglgdffkgcklhepbcfpbfd`。
6. 打开 **FanVPN AI Bridge** 的“详情”，将“网站访问权限”设为 **在所有网站上**。

Manifest 内置公开的开发 identity key，因此从不同目录加载时 ID 也保持不变。该 key 不是 API Key，也不包含私钥。

Chrome 可能在扩展重装、升级或策略变化后扣留 Manifest 已声明的站点权限。此时 Gemini/OpenAI 可能仍返回普通 HTTP 认证错误，但 `chatgpt.com/backend-api/codex` 会在扩展上下文中表现为 `Failed to fetch`。扩展弹窗的“ChatGPT 网站权限”必须显示“已授权”；`diagnose.ps1` 中 `chatgpt_site_access_granted` 也应为 `true`。

## 3. 注册 Native Host

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

默认同时注册当前用户登录任务 `FanVPN Bridge Bootstrap`。如果只想注册 Native Messaging Host，可显式传入 `-SkipStartupTask`。

脚本会生成带绝对 EXE 路径和固定扩展 origin 的 manifest，并写入：

```text
HKCU\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge
```

随后在扩展页面点击刷新。扩展弹窗应显示：

- Native Host：已连接
- 协议握手：完成
- 浏览器执行器：offscreen

安装脚本还会把 `127.0.0.1,localhost` 合并进用户级 `NO_PROXY`，避免设置了 `HTTP_PROXY/HTTPS_PROXY` 的程序把本地网关请求错误发送给外部代理。已有 `NO_PROXY` 条目不会丢失。安装后请重启 VS Code；如不希望修改该环境变量，可传入 `-SkipNoProxy`。

## 4. 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:18888/__bridge/health
```

也可以使用更直观的本地诊断端点：

```powershell
Invoke-RestMethod http://127.0.0.1:18888/health -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/routes -Proxy $null
```

重启恢复、启动日志、停止和回滚见 [RECOVERY_WINDOWS.md](RECOVERY_WINDOWS.md)。

正常结果的关键字段：

```json
{
  "status": "ok",
  "native_channel_connected": true,
  "executor": "offscreen"
}
```

也可运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\diagnose.ps1
```

## 5. 路由配置

运行时读取 `dist\fanvpn-bridge\routes.json`。修改后刷新桥接扩展，让 Chrome 重启 Native Host。

不要把 API Key 写进 `routes.json`；Key 继续由 Codex、Claude Code 或 CC Switch 管理并作为 HTTP header 发送。

## 6. 卸载

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

然后在 Chrome 扩展页面移除 FanVPN AI Bridge。卸载脚本不会删除 FanVPN 扩展，也不会删除仓库或构建产物。

## 故障定位

| 现象 | 层 | 处理 |
|---|---|---|
| ChatGPT 网页可开，但 `chatgpt-codex` 返回 502 | Chrome 站点权限 | 在 FanVPN AI Bridge 详情中将网站访问设为“在所有网站上”，然后刷新扩展 |
| 弹窗显示 Native Host 未连接 | 安装/注册 | 重新运行 `install.ps1`，刷新扩展 |
| health 端口拒绝连接 | Native Host 未运行 | 检查扩展是否启用、ID 是否一致 |
| `EGRESS_UNAVAILABLE` | Offscreen | 刷新扩展并查看扩展错误 |
| `UPSTREAM_CONNECTION_FAILED` | Chrome/FanVPN/上游 | 先用普通 Chrome 标签确认 FanVPN 已连接 |
| 上游返回 Gemini 400 signature | CC Switch 转换 | 在 `cc-switch` 仓库检查 thought signature 状态 |
