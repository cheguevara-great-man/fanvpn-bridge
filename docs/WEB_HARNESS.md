# ChatGPT 网页执行器

本功能在 `codex/web-harness` 分支提供预览版。已完成本地代码检查、回归测试、Windows 打包启动检查，以及真实账号的 Tunnel、Connector、流式回答和本机工具调用验收。仍建议先在非关键任务中使用。

网页执行器是第三条模型链路：本机登录 ChatGPT 网页，Codex 继续管理任务、工具与审批。
原生 GPT 使用既有浏览器或服务器中心链路；Gemini 使用既有账号适配器。

## 上游来源

- 仓库：https://github.com/miuuyy/codex-chatgpt-web
- Bridge 发行版本：v5.0.13
- 导入基线：v5.0.4
- 固定提交：c648c09501bb1b704c7ad5273fb5f5d6b8992dd2
- 源码：`third_party/web-harness`，通过 Git subtree 导入，保留 MIT 与第三方许可证。

## 集成边界

- 产品执行文件名使用 WebHarness，不含 VPN；品牌名称不改变安全软件的检测结果。
- 上游运行目录、浏览器会话和管理配置独立保存。
- 上游安装器只能修改执行器自己的配置副本。用户真实 Codex 配置由 Bridge 统一管理。
- 自动模式和手动模式共享当前轮次的工具能力校验；不得关闭 Codex 审批以改善兼容性。
- 网页模式不计入官方 Codex Credits。上游估算的 token 不能当成官方账单。
- 模型切换必须处理网页压缩检查点、响应 ID 和原生推理数据的边界。
- Gateway 的 Chrome 代理不会自动作用于 Electron，网页执行器需要显式的网络配置。

## 交付验收

运行时打包、配置隔离、流式传输与取消、模型目录、工具调用、压缩、多任务隔离、
安装更新与回滚通过之后，才标记为可部署。网页登录及 Connector 授权须由账号持有人完成。

## 使用入口

需要同时更新本分支的 Chrome 扩展和 Native Host（3.9.0）。只替换扩展目录不能更新后端。

1. 在 Bridge 弹窗的「ChatGPT 网页执行器」中点击「安装 / 更新 WebHarness」。
2. 选择网络：本机已有可用系统代理时使用系统网络；没有 Clash 时可选择「使用已保存的服务器代理」。后者读取 `%LOCALAPPDATA%\FanVPNBridge\direct-proxy.json`，缺少该文件时必须先配置服务器凭据，不会悄悄使用其他服务器。
3. 点击「打开 WebHarness」，在独立浏览器会话中登录自己的 ChatGPT。
4. 在执行器向导中配置 Secure MCP Tunnel 和 Connector，然后选择自动模式或手动模式（Zero Risk）。两种模式的 Connector 和凭据独立，不能混用。
5. 点击「启用 ChatGPT Web 模式」。该按钮进入 Hybrid Native 统一路由，但会保存进入前的 Direct 模型和配置。点击「配置完成后刷新网页模型」，重启 Codex 后选择 `chatgpt-web/` 模型。
6. 退出网页模型时，在 Bridge 弹窗点击原来的「服务器直连」「浏览器精简」或「浏览器完整」。如果当前仍选中 `chatgpt-web/*` 或 Gemini，Bridge 会自动恢复进入 Hybrid 前的 GPT，避免把网页模型误发给官方 Codex 后端。

自动模式由执行器操作自己的 ChatGPT 页面；手动模式需要用户粘贴并发送提示词，再确认已发送。
「Zero Risk」是上游模式名称，不是零账号风险、零安全风险的保证。
网页套餐是否具备 Connector 权限，需要用实际账号验收；不能仅凭 Plus/Pro 标签保证全部工具可用。

## 两条原链路与新入口

| 所选模型 | 本地入口 | 实际后端 |
|---|---|---|
| 原生 GPT | 原有 18888 或 18890 | 原浏览器链路或服务器执行器 |
| Gemini | 原有 18888 Hybrid | 既有 Google 账号适配器 |
| `chatgpt-web/*` | 18888 Hybrid 或 18890 | 本机 WebHarness 17841 |

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

## 诊断边界

本地测试覆盖路径校验、凭据隔离、分流和目录合并，不替代真实账号的工具调用验收。
网络设置为服务器代理时，Electron 页面使用 HTTPS 代理；子进程得到标准代理环境变量。
Secure MCP Tunnel 已在服务器代理模式下完成真实连接和工具调用验证；公司网络策略或服务器出口变化仍可能影响连接。
不要把 `config.json`、`network.json`、Cookie 或登录 token 发到日志或问题报告。
