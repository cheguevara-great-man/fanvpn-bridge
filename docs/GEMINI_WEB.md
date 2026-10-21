# Gemini 网页接入（实验功能，默认关闭）

这条线路使用 Gemini 网页账号，不是已有的 Antigravity / Code Assist Gemini。
模型名为 `gemini-web/<账号实际模型 ID>`；不会把 Gemini 4 静默降级为其他模型。

## 架构与边界

Codex → Bridge → 本机 Gemini-Web2API → Gemini 网页后端。

- 开源服务固定为 [Gemini-Web2API v0.1.2](https://github.com/Mag1cFall/Gemini-Web2API/releases/tag/v0.1.2)，下载摘要与提交记录在 `config/gemini-web-source.json`。
- 服务只接收文本请求。Codex 工具不传给它的 JSON 工具适配器，而由 Bridge 使用已有工具协议转换；脚本参数用 JSON，`apply_patch` 内容用原始 freeform。
- Bridge 不执行真实工具操作、不修补模型的 JSON 或闭标签。非法工具输出报错，不执行部分补丁。Bridge 只在工具发现时返回已有目录中的工具说明；文件、命令、插件操作仍由 Codex 执行。
- 编程工具及调用方直接提供的普通函数首轮发送完整定义；`mcp__` 插件工具只发送名称目录，通过 `bridge_load_tool_definitions` 按需加载完整说明和参数。一次加载 1–8 个具名工具，必须单独请求，不能与真实工具调用混发。未加载工具不能执行；每个客户端请求最多允许 4 轮定义加载，超限明确报错。
- 已加载定义随网页会话复用。普通续聊只补充新消息、工具结果和短格式提醒，不重复工具目录、调用示例或参数 schema；会话缓存明确丢失时，重新发送编程工具及已加载插件工具的完整定义，而不是整个插件目录。其他模型线路不受影响。
- 插件目录按 namespace 分组，前缀只写一次；模型加载时仍必须输出完整限定名称，Bridge 不补全简称。续聊固定格式提醒约 663 字符，实际新增消息和工具结果另算。基础指令、工作区规则和必需工具参数不裁剪。
- 首次使用此前未加载的插件工具需要额外模型请求；普通编程不需要。该内部请求不暴露为 Codex 可执行工具，所报用量会计入最终响应。工具说明缩短不能直接换算成 Google 额度的固定倍数，实际额度收益需由账号用量验证。
- 无工具轮次转发真实网络流；启用工具的轮次缓存完整答案后返回，等待期间发送 SSE 心跳。后者向开源服务请求非流式结果，避开该版本对网页文本快照改写的流式拒收。
- 同一已完成请求在带有 `previous_response_id` 或 `prompt_cache_key` 的情况下重放原结果和原调用 ID，不重新请求模型。正在生成时的重连合并尚未实现。
- 普通续聊使用网页会话。只有服务明确报告会话缓存丢失时，才用 Bridge 的完整历史恢复一次。网络错误、鉴权失败、限流不自动重发。
- Bridge 不因闲置 30 分钟丢弃历史；内存检查点最多保留 32 个响应，重启后不保留。稀疏续聊找不到历史时明确报错，不能假装仍有上下文。
- 支持文本、具名 function/custom 工具、包含这些工具的 namespace 和 plain text 输出。namespace 在提示词中使用限定名称，返回调用时保留 Codex 的 namespace/name 字段。不支持图片输入、内置 tool_search、强制工具选择或结构化输出；不支持的输入明确报错。
- 不实现 OpenAI 服务端内置 `web_search`。模型目录关闭其能力声明；Codex 默认设置仍附带该工具时，Bridge 不把它列入可执行工具，记录能力不可用日志，并在模型提示词中明确说明限制、禁止编造搜索过程和结果。普通聊天不因此被拒绝，普通搜索函数不受影响，其他模型和 Codex 全局搜索设置不变。这不等于关闭 Gemini 网页自身的联网功能，也不保证其联网事件已被适配。
- 模型目录缓存 60 秒，`GET /gemini-web/v1/models?refresh=force` 强制重读。只列账号可用的普通聊天模型，不暴露视频、图片、Task 等专用入口。
- 不读取 Chrome Cookie 或密钥，不上传登录文件，不修改代理，不自动部署或启动服务。

## 手动准备（不会部署）

在仓库目录运行：

```powershell
.\tools\prepare_gemini_web.ps1
# 下载需要经过现有本地代理时，可以显式指定；这不会修改系统代理：
# .\tools\prepare_gemini_web.ps1 -DownloadProxy http://127.0.0.1:18889
```

只下载、验证 SHA-256 并解压到 `dist-gemini-web/v0.1.2`；脚本不会执行 EXE。
源码接入需要你之后自行更新 Native Host，现有运行中的安装不会因此变化。

## 登录与启用（由用户自行操作）

1. 在独立 Chrome Profile 登录用于测试的 Google 账号。网页账号的权限、额度和地区要求仍然适用。
2. 阅读上游的 [认证说明](https://github.com/Mag1cFall/Gemini-Web2API/blob/v0.1.2/docs/protocol.md)。其 Chrome `setup` 会读取 App-Bound / 设备绑定登录材料，且运行服务会轮换 Cookie；共享浏览器登录会话是否受影响需要单独验证。不要在不了解后果时直接导入日常主账号，不要关闭浏览器安全机制，也不要把 Cookie 粘贴进聊天或 Git。
3. 在解压目录由你自行执行上游的 setup、启动服务。建议先单账号、单请求测试，不启用多账号调度。保持监听 `127.0.0.1:8007`，不要监听公网。上游默认临时聊天；若希望在网页历史里核对，启动前设置 `GEMINI_SAVE_HISTORY=true`。

   注意：Go 服务的网络请求不会自动经过 Chrome 的 Browser Gateway。需要代理时，在**该服务自己的环境**中设置上游支持的 `PROXY`（HTTP、HTTPS 或 SOCKS5），复用你已经可用的出口。例如已有且支持 Gemini 地区的本机 HTTP 代理可以填 `http://127.0.0.1:18889`。这不要求改变系统代理、切换 Chrome 节点或启动另一套网络模式；未启用的代理不能仅靠填写地址变得可用。登录配置和日常请求应使用同一出口。
4. 先验证服务的 `/v1/models`，确认你账号返回的是所需 Gemini 4；不要仅看 README 的模型名称。
5. 更新 Bridge 后，在其状态目录（Windows 为 `%LOCALAPPDATA%/FanVPNBridge`，无 `LOCALAPPDATA` 时为 `%USERPROFILE%/.fanvpn-bridge`）手动新建 `gemini-web.json`：

```json
{"base_url":"http://127.0.0.1:8007/v1"}
```

这是显式启用开关，仅包含本机地址，不包含 Google 登录凭证。也可以用 Native Host 进程环境变量 `FANVPN_GEMINI_WEB_URL`（优先级更高）。若上游启用了 `PROXY_API_KEY`，对应密钥通过 Native Host 的 `FANVPN_GEMINI_WEB_API_KEY` 提供；不要将 Google Cookie 当成这个本机访问密钥。

6. 由你自行重启 Native Host，使其读取开关，再刷新目录：

```powershell
Invoke-RestMethod 'http://127.0.0.1:18888/gemini-web/v1/models?refresh=force' -Proxy $null
.\tools\refresh_model_catalog.ps1
```

## 关闭与验证

工具说明优化版预编译 Native Host 存放在仓库 `dist-gemini-web-optimized-bridge/browser-ai-bridge`；编译到此独立目录不会覆盖当前运行的安装。Gemini 服务解压在 `dist-gemini-web/v0.1.2/gemini-web2api-v0.1.2-windows-amd64`。部署和账号配置由用户决定何时进行。

删除或移走自己创建的 `gemini-web.json`，取消对应环境变量，然后自行重启 Native Host、刷新目录。默认关闭时不会请求 Gemini 服务；这不影响其他模型线路。

真实验收至少覆盖：实际模型选择、普通流式、长中文补丁、多轮工具结果、取消、限流、服务重启/缓存过期、Bridge 重启和闲置后续聊。自动测试使用本机模拟服务；真实账号已完成一次 Gemini 4 的无害 namespace 工具返回验证，没有执行工具。这不能证明全部工作流、Google 登录长期稳定性或账号安全。

这是非官方网页协议；官网升级、风控、登录失效和套餐限制仍可能导致不可用。不要通过换账号、代理轮换或无休止重试绕过限流。
