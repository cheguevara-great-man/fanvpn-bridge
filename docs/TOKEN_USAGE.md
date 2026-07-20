# 多电脑 Codex 用量统计

## 1. 这个功能解决什么问题

Bridge 2.9.0 可以把多台 Windows 电脑通过**浏览器精简**或**浏览器完整**链路产生的 Codex
用量汇总到同一台 Browser Gateway 服务器。它适合“同一个人使用多台电脑，希望大致均衡各电脑
消耗”的场景。

系统能回答：

- 每台电脑在选定时间范围内用了多少请求、Token 和估算 Credits；
- 每天每台电脑用了多少；
- 每台电脑分别使用了哪些模型、推理档位和速度档位；
- 输入、缓存输入、输出、推理输出各是多少；
- 哪台电脑消耗偏高，其他电脑还可使用多少才能追平；
- 设置一个周期总预算后，每台电脑的平均目标和剩余建议是多少。

它**不能**读取 ChatGPT Pro 账号的官方实时剩余额度、滚动窗口或重置时间。那些数据仍以
Codex 的 `Settings > Usage` 页面为准。本项目计算的是基于官方费率的可审计估算值。

## 2. 数据链路

```text
Codex 模型响应
  → Bridge 只提取 response.usage、模型、推理档位和速度档位
  → 本机 SQLite outbox 先保存
  → 后台异步通过 Chrome / Browser Gateway 上报
  → 中央 SQLite 去重保存
  → HTTPS 网页按机器、日期和模型聚合
```

聊天回复会先返回给 Codex，上报随后在后台进行。因此中央服务器暂时离线不会拖慢当前回复；
未发送事件会留在本机，恢复连接后自动补报。

## 3. 每条事件包含什么

- 随机且稳定的 `machine_id`；
- 你设置的 `machine_name`；
- 请求完成时间和 Bridge 路由；
- 模型名称；
- 推理档位，例如 `low`、`medium`、`high`、`xhigh`；
- 服务速度档位，例如标准或 Fast；
- 输入、缓存输入、输出、推理输出和总 Token；
- 随机事件 ID，用于服务器去重。

不会上传提示词、回复正文、Cookie、OpenAI 登录 Token、API Key、文件内容、文件名或工作区路径。

## 4. Credits 如何计算

系统**不会调用某个 OpenAI 计费 API**。模型响应已经包含真实 `usage` 数据；中央服务器用代码中
版本化保存的官方费率表计算：

```text
未缓存输入 = 输入 Token - 缓存输入 Token

标准 Credits = (
  未缓存输入 × 模型输入费率
  + 缓存输入 × 模型缓存费率
  + 输出 Token × 模型输出费率
) ÷ 1,000,000

最终 Credits = 标准 Credits × 速度倍率
```

费率来源是 OpenAI 官方
[Codex Rate Card](https://help.openai.com/en/articles/20001106)，单位均为每 100 万
Token 的 Credits。服务器不会定时抓取网页；这样可以避免官方页面结构变化导致统计在无人知情时
改变。升级项目时会更新内置表，管理员也可以在网页“设置”页面覆盖费率。

### 推理档位怎么处理

同一个模型的 `low / medium / high / xhigh` 使用相同的每 Token 标准费率。更强档位通常会产生
更多推理输出 Token；这部分已经包含在模型返回的输出用量中，所以会自然增加 Credits，不需要
人为设置一个猜测倍数。

### Fast 模式怎么处理

Fast 是速度档位，不是推理档位。按照 OpenAI 当前
[Codex Speed 文档](https://learn.chatgpt.com/docs/agent-configuration/speed)：

- GPT-5.6、GPT-5.5 Fast：标准 Credits 的 `2.5×`；
- GPT-5.4 Fast：标准 Credits 的 `2×`；
- 其他没有公开 Fast 倍率的模型：不擅自加价。

Bridge 2.9.0 将推理档位和速度档位分别上报，避免旧版把两者混在同一个字段中。

未知或研究预览模型不会套用相近模型的费率，而会显示为“未定价 Token”；管理员确认官方费率后
可在设置页补充。

## 5. 为什么有三种 JSON 凭据

三种文件的权限不同，不应该合并：

| 文件 | 分发对象 | 包含能力 |
|---|---|---|
| `deployment.local.json` | 每台需要使用代理和上报的电脑 | Gateway 代理凭据、只写上报地址和上报 Token |
| `usage-viewer.local.json` | 每台需要看网页的电脑 | 只读网页地址、用户名和密码 |
| `usage-admin.local.json` | 只保留在管理电脑 | 管理员网页登录、汇总 API Token、修改预算和费率 |

普通电脑拿到 `deployment.local.json` 后只能提交自己的统计，不能读取全体数据；再给它一份
`usage-viewer.local.json`，就能用浏览器查看全部只读页面。不要把管理员文件复制到六台机器。

## 6. 在每台电脑启用上报

先把服务器部署生成的最新版 `deployment.local.json` 安全复制到目标电脑，例如：

```text
C:\Users\你的用户名\.browser-gateway\deployment.local.json
```

然后在 FanVPN Bridge 仓库根目录的 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_usage_reporting.ps1 `
  -GatewayCredentialPath "$HOME\.browser-gateway\deployment.local.json" `
  -MachineName '电脑-A'
```

六台电脑分别使用清楚且不重复的名称，例如 `公司-01` 到 `公司-06`。机器 ID 首次配置时随机生成，
之后改名或重复执行脚本不会改变 ID。配置完成后更新 Native Host，并重启 Chrome。

查看本机上报状态：

```powershell
Invoke-RestMethod http://127.0.0.1:18888/__bridge/usage -Proxy $null
```

关键字段：

- `pending_events`：尚未成功上报、等待补报的事件；
- `delivered_events`：本机保留的近期已送达记录数；
- `delivered_total_tokens`：已确认送达的总 Token。

## 7. 让每台电脑查看网页

把 `usage-viewer.local.json` 安全复制到需要查看的电脑。打开文件即可找到：

- `dashboardUrl`：网页地址；
- `dashboardUsername`：只读用户名；
- `dashboardPassword`：只读密码；
- `role`：固定为 `viewer`。

它不会自动登录，也不会修改 Chrome 代理。用户在浏览器打开地址并输入账号密码即可。只读账号能
访问总览、每日、机器、机器详情和模型页面，但不能修改预算与费率。

## 8. 页面说明

- **总览**：关键指标、最近每日趋势、主要模型组合和机器公平度；
- **每日**：按北京时间自然日列出 Token、Credits 和机器分布；
- **机器**：比较所有机器的缓存率、输入、输出、Credits 和主要模型；
- **机器详情**：单台机器的模型组合与每日历史；
- **模型**：模型、推理档位、标准/Fast、倍率、Token 构成、Credits 占比和标准费率；
- **设置**：仅管理员可设置周期预算和覆盖模型标准费率。

时间范围支持 7、30、90、180 和 366 天。公平建议只比较当前选择范围内有记录的机器。

## 9. PowerShell 汇总

管理电脑仍可使用 JSON API：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\get-usage-summary.ps1 `
  -CredentialPath "$HOME\.browser-gateway\usage-admin.local.json" `
  -Days 30
```

## 10. 停用与数据保留

在目标电脑运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_usage_reporting.ps1 -Disable
```

然后重启 Chrome。停用只删除该电脑的上报配置，不删除中央服务器已有历史。中央数据库位于服务器：

```text
/var/lib/browser-gateway/usage.sqlite3
```

建议把它纳入服务器备份。删除数据库会永久清空历史统计。

## 11. 当前边界

- 只统计经过 Browser AI Bridge 的浏览器精简和浏览器完整请求；直连模式绕过 Bridge，无法统计；
- Credits 是按公开费率计算的估算消耗，不是官方账号余额；
- OpenAI 可能修改费率或 Fast 规则，升级前后应查看官方 Rate Card；
- 旧版 2.7/2.8 队列仍可上报，但没有速度档位的历史事件按标准模式计算；
- 日期按北京时间自然日聚合，事件原始时间仍以带时区的 ISO 时间保存。
