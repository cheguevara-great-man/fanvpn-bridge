# Codex 用量上报客户端

Browser AI Bridge 负责在每台 Windows 电脑上采集并上报用量；中央存储、Credits 计算、额度分配、
网页和服务器运维属于 Browser Gateway。完整说明请阅读
[Browser Gateway 中央用量文档](https://github.com/cheguevara-great-man/browser-gateway/blob/main/docs/TOKEN_USAGE.md)。

## Bridge 做什么

- 从 Codex 模型响应的 `usage` 中提取 Token 数量、模型、推理档位和速度档位；
- 先写入本机 SQLite outbox，再异步上报，服务器离线不会拖慢当前回复；
- 每 30 秒读取 Codex 当前 Usage 窗口，并同步中央服务器的本机放行策略；只上传套餐、已用比例、窗口和重置时间等脱敏字段；
- 获取中央服务器为本机生成的额度策略；达到已启用的均分上限时，阻止下一次模型 POST。

不会上传提示词、回复正文、文件、工作区路径、Cookie、OpenAI 登录 Token 或 API Key。

## 在一台电脑上启用（推荐）

1. 管理员在 Browser Gateway 的中央统计网页进入“设备额度”，输入设备名称并生成一次性注册码。
2. 确认 Browser Gateway 扩展为 `0.3.0+`、FanVPN AI Bridge 与 Native Host 为 `3.4.0+`；在目标电脑
   打开 Browser Gateway 插件，填写统计服务器和注册码，点击“注册这台设备”。
3. Browser Gateway 插件会同时保存代理配置，并通过受限的扩展间消息把设备身份交给 FanVPN AI
   Bridge；Native Host 原子写入 `usage-reporting.json` 后自动重连。
4. 以后点击 Browser Gateway 插件中的“打开用量统计”，即可使用设备只读身份查看网页。

所有电脑的设备权限相同：使用 Gateway、上报自己的用量、只读查看统计。管理员权限只属于中央网站
的管理员账号，与具体电脑无关。

## 旧版文件方式

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
