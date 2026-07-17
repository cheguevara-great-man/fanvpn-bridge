# 多电脑 Token 用量统计

## 能统计什么

Bridge 2.7.0 可以把多台电脑的 Codex Token 用量汇总到同一台 Browser Gateway
服务器。每条记录只包含：

- 随机机器 ID 和自定义机器名称；
- 请求完成时间、模型和 Bridge 路由；
- 输入、缓存输入、输出、推理输出和总 Token 数。

不会收集或上传提示词、回复正文、Cookie、OpenAI Token、API Key、文件内容或工作区路径。
事件 ID 在服务器端唯一，网络重试不会重复计数。

统计值来自模型响应中的官方 `usage` 字段，不按照字节数估算。目前准确覆盖
**浏览器精简**和**浏览器完整**模式。服务器直连模式绕过 Browser AI Bridge，因此不在这里统计；
如果需要统计六台电脑，应让这些电脑使用浏览器模式。

## 首次部署

先用最新版 Browser Gateway 重新执行服务器部署。部署脚本会额外安装独立的 HTTPS
统计服务，并在以下两个本地文件中保存不同权限的凭据：

- `~/.browser-gateway/deployment.local.json`：分发给各电脑，只含上报权限；
- `~/.browser-gateway/usage-admin.local.json`：只留在管理电脑，可读取汇总。

然后在每台需要统计的 Windows 电脑上更新 Bridge，并在仓库根目录的 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_usage_reporting.ps1 `
  -GatewayCredentialPath "$HOME\.browser-gateway\deployment.local.json" `
  -MachineName '电脑-A'
```

每台电脑使用不同且容易辨认的 `MachineName`。脚本首次运行会生成稳定的随机机器 ID，
以后改名或重复配置不会改变这个 ID。配置完成后重启 Chrome，使 Native Host 重新加载。

本机状态可通过下面的命令查看：

```powershell
Invoke-RestMethod http://127.0.0.1:18888/__bridge/usage -Proxy $null
```

`pending_events` 表示因中央服务器暂时不可用而留在本机等待补报的事件。聊天响应不会等待上报。

## 查看中央汇总

在保留管理凭据的电脑上运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\get-usage-summary.ps1 -Days 30
```

输出会按电脑列出请求数、输入/缓存/输出/总 Token 和最后上报时间，并给出所有电脑总计。

## 停用

在目标电脑运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_usage_reporting.ps1 -Disable
```

然后重启 Chrome。停用只删除本机上报配置，不删除服务器已有的历史汇总。
