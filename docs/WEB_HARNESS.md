# ChatGPT 网页执行器

本功能在 `codex/web-harness` 分支提供预览版。已完成本地代码检查、回归测试、Windows 打包启动检查，以及真实账号的 Tunnel、Connector、流式回答和本机工具调用验收。仍建议先在非关键任务中使用。

网页执行器是第三条模型链路：本机登录 ChatGPT 网页，Codex 继续管理任务、工具与审批。
原生 GPT 使用既有浏览器或服务器中心链路；Gemini 使用既有账号适配器。

## 上游来源

- 仓库：https://github.com/miuuyy/codex-chatgpt-web
- Bridge 发行版本：v5.0.6-bridge.1
- 当前上游基线：v5.0.6（完整合并；最初导入为 v5.0.4）
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

## 交付验收

运行时打包、配置隔离、流式传输与取消、模型目录、工具调用、压缩、多任务隔离、
安装更新与回滚通过之后，才标记为可部署。网页登录及 Connector 授权须由账号持有人完成。

## 使用入口

需要同时更新本分支的 Chrome 扩展和 Native Host（3.10.3）。只替换扩展目录不能更新后端。

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

### 第二轮 `missing cwd` 的目录隔离修复（bridge.5）

安装配置仍写入 `integration`，但线程环境的只读校验必须查询真实 Codex home。
启动器在隔离 `CODEX_HOME` 前保存 `BRIDGE_WEB_CODEX_AUTHORITY_HOME`；运行时只在
历史 rollout 与当前任务 visualization 根目录校验中使用它，不用于安装、登录或修改用户配置。
后续请求可以省略初始环境，运行时按线程 ID、当前 turn ID、工作区和权限校验原生记录后恢复；
不从任意提示词或其他线程借用目录，不放宽沙箱。

Bridge 清理网页模型历史中的本地 reasoning 项时也必须保留消息 ID，避免破坏原生环境来源标记。
模型目录刷新合并已知原生模型缓存，避免模式切换或一次刷新失败把已发现的 GPT 模型删除。

2026-09-09 本机验收：失败的原生 Codex 会话重启执行器后可恢复历史；独立测试会话连续三轮
完成 `apply_patch` 创建文件、终端读取、`apply_patch` 修改并再次读取。只读测试会话仍拒绝写入。
另有 52 项环境/集成测试、28 项 Bridge/模型目录测试和 20 项启动器界面连线测试通过。
测试没有覆盖所有模型、所有插件或公司设备，不应把该结果视为所有网页异常均已消除。
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
