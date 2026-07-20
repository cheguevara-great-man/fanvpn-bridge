# Codex 用量上报客户端

Browser AI Bridge 负责在每台 Windows 电脑上采集并上报用量；中央存储、Credits 计算、额度分配、
网页和服务器运维属于 Browser Gateway。完整说明请阅读
[Browser Gateway 中央用量文档](https://github.com/cheguevara-great-man/browser-gateway/blob/main/docs/TOKEN_USAGE.md)。

## Bridge 做什么

- 从 Codex 模型响应的 `usage` 中提取 Token 数量、模型、推理档位和速度档位；
- 先写入本机 SQLite outbox，再异步上报，服务器离线不会拖慢当前回复；
- 每 5 分钟读取 Codex 当前 Usage 窗口，只上传套餐、已用比例、窗口和重置时间等脱敏字段；
- 获取中央服务器为本机生成的额度策略；达到已启用的均分上限时，阻止下一次模型 POST。

不会上传提示词、回复正文、文件、工作区路径、Cookie、OpenAI 登录 Token 或 API Key。

## 在一台电脑上启用

把服务器生成的 `deployment.local.json` 放到：

```text
C:\Users\你的用户名\.browser-gateway\deployment.local.json
```

在 Bridge 仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_usage_reporting.ps1 `
  -GatewayCredentialPath "$HOME\.browser-gateway\deployment.local.json" `
  -MachineName '公司-01'
```

每台电脑使用不重复的名称。首次配置生成稳定的随机机器 ID；改名或重新运行脚本不会改变 ID。
随后更新 Native Host 并重启 Chrome。

## 检查状态

```powershell
Invoke-RestMethod http://127.0.0.1:18888/__bridge/usage -Proxy $null
```

重点字段：

- `pending_events`：尚未送达中央服务器的事件；
- `delivered_events`：近期已确认送达的事件；
- `policy`：本机当前额度策略和更新时间。

停用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_usage_reporting.ps1 -Disable
```

停用只删除本机上报配置，不删除服务器历史。

## 边界

- 只统计经过 Browser AI Bridge 的浏览器精简和浏览器完整请求；直连模式无法统计；
- 额度产品接口不是公开稳定 API，读取失败时继续聊天，不会上传完整响应；
- 中央服务器不可达时沿用最后一次策略；从未取得策略时默认放行；
- 最后一个任务完成后才知道实际 Credits，因此硬上限可能有少量超出。
