# ChatGPT 网页执行器

网页执行器是第三条模型链路：本机登录 ChatGPT 网页，Codex 继续管理任务、工具与审批。
原生 GPT 使用既有浏览器或服务器中心链路；Gemini 使用既有账号适配器。

本文只描述当前安装、使用、文件位置和限制。历史修复与逐版本验收记录统一放在
[问题与解决记录](PROBLEM_SOLVING.md)。

## 上游来源

- 仓库：https://github.com/miuuyy/codex-chatgpt-web
- 当前上游基线：v5.0.6（完整合并；最初导入为 v5.0.4）
- 当前 Bridge 发行版本：v5.0.6-bridge.3。Windows 安装包已内置并校验 OpenAI tunnel-client v0.0.12；并兼容 Codex 0.153+ 在自动压缩后发送的不含 cwd 的当前环境差量。目标电脑配置 MCP 时不再临时访问 GitHub 下载该文件。
- 固定提交：e85e3693fdb4e3e033348c08df0298c20fcdb612
- 旧版显示的 5.0.13 是本项目自行递增的包版本，并不代表上游版本。
- 源码：`third_party/web-harness`，通过 Git subtree 导入，保留 MIT 与第三方许可证。

## 集成边界

- 产品执行文件名使用 WebHarness，不含 VPN；品牌名称不改变安全软件的检测结果。
- 上游运行目录、浏览器会话和管理配置独立保存。
- 上游安装器只能修改执行器自己的配置副本。用户真实 Codex 配置由 Bridge 统一管理。
- 自动模式和手动模式共享当前轮次的工具能力校验；不得关闭 Codex 审批以改善兼容性。
- 网页模式不计入官方 Codex Credits。上游估算的 token 不能当成官方账单。
- 模型切换必须处理网页压缩检查点、响应 ID 和原生推理数据的边界。
- Gateway 的 Chrome 代理不会自动作用于 Electron，网页执行器需要显式的网络配置。

## 安装与使用

需要同时更新本分支的 Chrome 扩展和 Native Host（3.10.5）。只替换扩展目录不能更新后端。

新电脑先按[安装与升级](INSTALL_AND_UPDATE.md)完成一次 Native Host 注册。旧版迁移也应先更新 Bridge
扩展和 Native Host，再安装或更新 WebHarness；不要只替换扩展目录或只修改 WebHarness 版本字段。
账号登录、Tunnel 与 Connector 授权需要在实际使用电脑上完成，不要复制别人的 Cookie、API key 或本机私密配置。

1. 在 Bridge 弹窗的「ChatGPT 网页执行器」中点击「安装 / 更新 WebHarness」。
2. 选择网络：本机已有可用系统代理时使用系统网络；没有 Clash 时可选择「使用已保存的服务器代理」。后者读取 `%LOCALAPPDATA%\FanVPNBridge\direct-proxy.json`，缺少该文件时必须先配置服务器凭据，不会悄悄使用其他服务器。
3. 点击「打开 WebHarness」，在独立浏览器会话中登录自己的 ChatGPT。
4. 在执行器向导中配置 Secure MCP Tunnel 和 Connector，然后选择自动模式或手动模式（Zero Risk）。两种模式的 Connector 和凭据独立，不能混用。
5. 在 Bridge 弹窗的“Codex 模型模式”选择“GPT + Gemini + WebGPT”。原生 GPT 的请求链路可继续独立选择官方直连、浏览器完整或服务器中心。
6. 点击“配置完成后刷新网页模型”，再点击“应用配置并启动 VS Code”。重启后的 Codex 模型菜单可直接在原生 GPT、Gemini 和 `chatgpt-web/*` 之间切换，不需要为了网页模型来回覆盖 Provider。

自动模式由执行器操作自己的 ChatGPT 页面；手动模式需要用户粘贴并发送提示词，再确认已发送。
「Zero Risk」是上游模式名称，不是零账号风险、零安全风险的保证。
网页套餐是否具备 Connector 权限，需要用实际账号验收；不能仅凭 Plus/Pro 标签保证全部工具可用。

## 两条原链路与新入口

| 所选模型 | 本地入口 | 实际后端 |
|---|---|---|
| 原生 GPT | 18888 Hybrid | 按弹窗选择进入官方直连、浏览器完整或服务器中心 |
| Gemini | 原有 18888 Hybrid | 既有 Google 账号适配器 |
| `chatgpt-web/*` | 18888 Hybrid | 本机 WebHarness 17841 |

网页模型不使用服务器上登录的账号，也不会经过官方 Codex Credits 上报器。
本地文件、终端工具和审批仍由 Codex 管理；不增加自动批准权限。
默认保留 Codex 原生子 Agent 协议，不把真实配置强制降为 V1。
跨模型子 Agent 若携带不透明的加密协议数据，上游可能拒绝；不能把本集成理解成任意混合子 Agent 已验证。

## 文件与升级

- 安装根目录：`%LOCALAPPDATA%\BrowserAIBridge\WebHarness`。
- `versions\<sha256>`：按内容哈希隔离的安装版本，不覆盖旧版本。
- `installation.json`：当前版本与上一版本路径。
- `config.json`、`browser`：执行器配置与独立网页登录状态。
- `integration`：上游专用 Codex 配置副本，不是用户真实 `.codex\config.toml`。
- `network.json`：仅本机保存的代理凭据，不能分享或提交 Git。
- `.codex\browser-ai-bridge-web-models.json`：已配置网页模式的模型缓存；Hybrid 自动更新时保留这些行。

升级前退出 WebHarness；新包校验或解压失败时不切换安装指针。旧版本不自动删除。
发布包采用 SHA-256 校验，但未签名包仍可能被系统或企业安全软件拦截；应走正常的软件审批，不关闭安全保护。
上游自动更新在此发行版中关闭，避免将 Bridge 适配覆盖为原版。

## 构建

在项目目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_web_harness.ps1 -Bun '完整路径\bun.exe'
```

需要 Windows、Node.js、Bun；依赖按上游锁文件安装。产物位于 `dist-web-harness`，包含 Windows ZIP 和校验清单。
扩展安装按钮读取本仓库对应的 WebHarness 发布资产；发布资产不可用时按钮会明确报错，不会覆盖当前可用版本。

## 当前限制与诊断边界

本地测试覆盖路径校验、凭据隔离、分流和目录合并，但不能替代真实账号的网页登录、Connector、
工具调用和公司网络环境验收。
网络设置为服务器代理时，Electron 页面使用 HTTPS 代理；子进程得到标准代理环境变量。
Secure MCP Tunnel 已在服务器代理模式下完成真实连接和工具调用验证；公司网络策略或服务器出口变化仍可能影响连接。
不要把 `config.json`、`network.json`、Cookie 或登录 token 发到日志或问题报告。
