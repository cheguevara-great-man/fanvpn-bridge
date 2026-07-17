# 架构

## 目标与边界

FanVPN Bridge 把本机开发工具发出的 HTTP 请求送入 Chrome，再利用 Chrome 中
FanVPN 已建立的浏览器网络出口访问预先允许的 AI API。

系统分为三层：

1. **客户端层**：Codex、Claude Code、CC Switch 等产生具体 AI 协议请求。
2. **传输层**：FanVPN Bridge 选择静态路由、传输 HTTP 字节并报告网络状态。
3. **浏览器出口层**：Chrome 扩展通过 Offscreen Document 执行 `fetch`，请求由 FanVPN 承载。

Bridge 不承担 Responses、Chat Completions、Anthropic Messages 或 Gemini Native
之间的转换，也不保存模型会话状态。Gemini 工具调用所需的 `thoughtSignature`
由 CC Switch 等协议适配器处理。

## 运行时拓扑

```text
Windows 用户登录
  └─ FanVPN Bridge Bootstrap 计划任务
      └─ 启动 Chrome
          └─ MV3 Service Worker 调用 runtime.connectNative()
              └─ browser-ai-bridge.exe
                  ├─ stdin/stdout：Native Messaging
                  └─ 127.0.0.1:18888：HTTP Gateway

本地客户端
  └─ HTTP Gateway
      └─ Native Messaging 分片协议
          └─ Chrome Offscreen Document
              └─ fetch → FanVPN → 固定 HTTPS 上游
```

Chrome 的 Native Messaging Port 维持 Host 和扩展 Service Worker 的生命周期。
端口断开后扩展按退避策略重连；Host 收到 stdin EOF 后停止接收请求、取消在途请求并退出。

三模式控制复用同一条受信 Native Messaging 通道：

```text
Chrome 扩展弹窗
  -> control.mode.get / control.mode.set
  -> Native Messaging
  -> CodexModeController
  -> 固定的 start_vscode_network_mode.ps1
  -> 事务式更新托管配置并启动 VS Code
```

扩展只能提交 `direct`、`browser_lean`、`browser_full` 三个枚举值，不能指定命令、脚本路径或任意
参数。启动器在修改前快照 Codex 配置、VS Code 设置、备份文件和端点状态；任何配置步骤或 VS Code
启动失败时恢复快照，并恢复切换前的 Direct 代理进程状态。

## 组件

### Native Host

`native-host/fanvpn_bridge/` 包含：

- `config.py`：严格解析监听、协议和路由配置。
- `routing.py`：将本地 route 映射到固定 HTTPS 上游。
- `framing.py`：Chrome Native Messaging 的 4-byte length framing。
- `protocol.py`：版本化 envelope、分片、Base64 和流控。
- `dispatcher.py`：按 request id 隔离并发请求、超时和取消。
- `http_server.py`：loopback HTTP/1.1 网关与诊断端点。
- `product_cache.py`：按账号和 Authorization 摘要隔离、有限容量、仅进程内存在的只读产品元数据缓存。
- `mode_control.py`：读取托管模式，只允许通过固定启动器切换三种 VS Code 网络模式。
- `runtime_logging.py`：脱敏、轮转的本地运行日志。
- `codex_login.py`：一次性 OAuth PKCE 登录、loopback 回调、凭据备份和原子写入。
- `main.py`：组合根和进程生命周期。

### Chrome 扩展

`chrome-extension/src/` 包含：

- `background.js`：Native Port、握手、重连、Offscreen 生命周期和受限模式控制消息。
- `offscreen.js`：组装请求、执行 fetch、处理取消和回传响应。
- `stream.js`：立即转发响应 chunk，并用独立空 frame 表示结束。
- `protocol.js`：浏览器端协议常量和校验。
- `popup.js`：连接、握手、执行器、站点权限、上次托管配置和三模式启动按钮。

### 构建布局

PyInstaller 工具、work 和 spec 缓存默认位于
`%LOCALAPPDATA%\BrowserAIBridge\build-cache`，不属于源码工作区。工具按固定 PyInstaller 版本复用，
work/spec 按仓库绝对路径摘要隔离。最终部署产物仍写入工作区的 `dist` 或 `dist-a` / `dist-b`，由
A/B 更新流程负责验证和切换；构建缓存与当前注册的运行槽位相互独立。

## 本地 HTTP 接口

客户端使用 `http://127.0.0.1:18888/{route}`。`route` 必须存在于运行时
`routes.json`，客户端不能提供任意上游 URL。

`chatgpt-codex` 承载登录后的 Codex 模型请求和模型目录。Browser Lean 只接管这条模型数据通道，
同时在 Codex 配置中关闭 Apps、插件同步、远程插件目录和分析请求。`auth-openai` 承载 Token
Exchange、刷新和注销；普通 API 请求不会进入该路由。

账号产品后端属于独立的控制面，包括插件安装状态、Apps/连接器、任务元数据和账号信息。
Browser Full 通过固定的 `chatgpt-backend` 路由转发这组请求。它与 Lean 共用同一套 Host 和扩展，
差别只在 Codex 与 VS Code 配置。Codex app-server 使用 `18888/chatgpt-backend/backend-api/`；
VS Code 扩展 WebView 使用官方扩展内置的 localhost 开发入口 `127.0.0.1:8000/api/`。Host 将后者
固定改写为前者对应的 `/backend-api/` 路径，拒绝 8000 端口上的所有非 `/api` 请求，因此不会
形成开放代理。

官方 Codex 会主动阻止把 ChatGPT MCP 凭据发给非 `chatgpt.com` origin。Host 仅在静态 route
解析后确认上游仍是 ChatGPT 官方产品后端，且路径属于托管 MCP 或 `/backend-api/wham/` 时，
从当前 `~/.codex/auth.json` 按请求读取访问令牌和账号 ID 并补齐缺失认证头。客户端自带的认证头
优先，凭据不会进入配置文件或诊断日志。

产品诊断默认关闭。Safe 模式记录路径、query 名和 header 名；Full 模式记录完整 URL、非敏感 header
值和 4xx/5xx 响应前 4 KiB。两种模式都有请求 ID，且始终遮盖认证凭据，不记录模型请求正文。

Browser Full（浏览器完整，实验）对产品控制面采用受限本地响应、优先级调度、短期缓存和硬性截止：
MCP 客户端按 Streamable HTTP 协议发出的可选 GET 由 Host 本地返回 405，`.well-known` 探测也在
本地结束，正式 MCP POST 仍原样转发。

Host 只缓存经过认证、明确列入 allowlist、返回 HTTP 200、允许缓存且不超过大小上限的 JSON GET：

| 路径 | TTL | 单条上限 |
|---|---:|---:|
| `/backend-api/ps/plugins/list?scope=GLOBAL` | 10 分钟 | 8 MiB |
| `/backend-api/ps/plugins/installed?scope=GLOBAL` | 30 秒 | 4 MiB |
| `/backend-api/ps/plugins/suggested?scope=GLOBAL` | 5 分钟 | 4 MiB |
| `/backend-api/plugins/featured` | 5 分钟 | 4 MiB |
| `/backend-api/connectors/directory/list` | 10 分钟 | 8 MiB |
| `/backend-api/wham/accounts/check` | 2 分钟 | 1 MiB |

缓存最多 256 条、总计最多 64 MiB，键由账号摘要、Authorization 摘要、完整上游 URL 和全部客户端
请求头摘要计算；原始 Token、Cookie 不进入缓存。只接受 JSON，且拒绝 `Set-Cookie`、
`no-store/no-cache`、`Pragma: no-cache` 和 `Vary: *`。相同键的并发 miss 合并为一次上游请求，18888 与 8000 的等待者共用
结果。缓存只存在于 Host 进程内，过期或 Host 退出后立即消失。已安装插件的只读状态快照最多陈旧
30 秒；插件安装/卸载等修改请求、Statsig POST、MCP、工具调用和模型响应均不缓存。

扩展使用三级调度：账号初始化、模型、MCP 和工具请求最高；插件元数据居中，普通查询最多 3 个并发，
建议/精选查询可使用第 4 个保留槽；大体积、分页的全局插件目录最低且只有 1 个并发槽。更高优先级
到达时，元数据 GET 可在响应头返回前安全让路。固定 ChatGPT 主机上明确允许的插件和连接器元数据
GET 每次等待响应头最多 10 秒；网络失败最多执行两次，调度抢占另有最多 4 次重新开始额度。从进入
调度、排队、抢占到重试结束还有独立的 15 秒硬性总期限。额度用尽统一返回可重试的请求超时，
而不是误报用户取消。账号检查可缓存但不自动重试，任何 POST 都不会自动重发。

首次登录使用独立的一次性助手：助手按 Codex 官方 OAuth 参数生成 PKCE 和 `state`，
打开 Chrome 官方授权页，在 `127.0.0.1` 临时端口接收授权码，然后只把 Token Exchange
请求送入 `auth-openai`。成功后凭据按 Codex 当前格式原子写入 `~/.codex/auth.json`；
旧文件先备份。Native Host 不保存该文件；Browser Full 只会按上述固定产品接口读取现有凭据，
日志不记录授权码或 Token。

管理端点：

- `GET /health` 或 `GET /__bridge/health`：本地进程和通道状态。
- `GET /ready`：Native Messaging 与 Offscreen 都可用时返回 200。
- `GET /routes`：返回已加载的脱敏路由列表。
- `POST /__bridge/probe/{route}`：执行无凭据出口探测。
- `GET /__bridge/version`：返回 Host 和协议版本。

支持 GET、POST、PUT、PATCH、DELETE、OPTIONS 和 HEAD。上游 HTTP 4xx/5xx 原样
返回；只有未得到有效 HTTP response head 的传输故障才由 Bridge 生成网关错误。

## Native Messaging 协议

规范位于 `contracts/native-messaging-v1.schema.json`：

- 首次消息使用 `hello` / `hello_ack` 协商版本。
- Host 与 Offscreen 实际协商 chunk 和流控窗口；单个原始 body chunk 最大 256 KiB。
- 每个 body frame 具有单调递增的 `seq`。
- 默认每个方向最多 4 个未确认 body frame，协议支持 1–16，使用累计 `flow.ack` 背压。
- head、body 和 end 分离，响应 chunk 收到后立即转发。
- 响应 ACK 在本地客户端实际消费 frame 后发送；慢客户端只暂停自己的请求，不阻塞其他请求。
- 客户端断开时发送 `request.abort`，浏览器调用 `AbortController.abort()`。
- Native Port 断开时 Offscreen 取消整批在途 fetch 并释放流控等待器。
- 成功的 `response.head` 和返回响应头前的 `error` 都可携带无敏感信息的 `timing`：
  `executor_queue_ms`、`fetch_head_ms`、`attempts`、`preemptions`。其中 fetch 时间是所有实际尝试的
  累计值，抢占也计为一次实际尝试；executor queue 是浏览器总执行时间减去该累计值，主要反映
  排队、让路后的等待和调度开销。
- `control.mode.get`、`control.mode.set` 和 `control.mode.result` 只传输固定模式枚举、结果和安全的
  用户提示，不允许把通用命令执行能力暴露给扩展。

## HTTP 透明性

- 请求体和响应体作为字节流传输，不解析 JSON。
- 删除 hop-by-hop header。
- 默认保留端到端 header；路由可配置请求头 allowlist。
- Offscreen Fetch 自动解压响应，因此 Bridge 删除不再有效的上游
  `Content-Encoding` 和 `Content-Length`，使用 chunked response 返回。
- 当前请求体会在浏览器 fetch 前组装，最大 32 MiB；响应体不会整体缓存。
- 请求体直接以 `Blob` 交给 fetch，响应分片使用零拷贝视图，避免额外的整块内存复制。
- 同时最多处理 16 个请求；超大 `Content-Length` 在进入 Chrome 前直接拒绝。

## 安全模型

- 监听地址必须是 `127.0.0.1`。
- 只接受指向当前 loopback 端口的 Host，拒绝带 Origin 的浏览器跨站请求，并删除上游 CORS 响应头。
- 上游只能来自静态配置且必须使用 HTTPS；测试 fixture 除外。
- 浏览器 fetch 不自动跟随重定向，避免凭据或请求体离开静态上游。
- `18888` 不支持 CONNECT、开放转发、目标 header 覆盖或浏览器 Cookie 复用。
- `8000` 只接受 loopback 的 `/api` 路径，并固定映射到 `chatgpt-backend/backend-api`。
- Offscreen 不可用时失败关闭，不回退到 Service Worker 或系统直连。
- 日志不记录请求正文、认证头、Token、Cookie 或 API Key。
- 经 Chrome 执行的成功请求日志记录 route、method、HTTP 状态、响应头/首段数据/结束耗时，以及浏览器排队、累计
  fetch、尝试和抢占时序；返回响应头前失败时以 `browser_fetch_failed` 记录相同浏览器时序，再记录
  `request_failed`。本地快速响应和缓存命中没有 browser fetch，不带这组时序。常规日志不记录
  URL、query、正文或凭据。

## 当前限制

- 本地请求需要 `Content-Length`，不接受 chunked request body。
- 请求体浏览器侧上限为 32 MiB。
- 尚未实现 WebSocket；Responses API 和 Anthropic 使用 HTTP/SSE。
- Offscreen API 没有专用于网络出口的 reason，当前使用兼容的扩展上下文。
- Bridge 依赖 Chrome 正在运行、扩展已授权且 FanVPN 节点可用。

## 可选的 VS Code 直连模式

直连模式与上述 `18888` 浏览器桥接是两条独立链路：

```text
VS Code（仅由专用启动器启动）
  -> HTTP_PROXY / HTTPS_PROXY + VS Code --proxy-server
  -> 127.0.0.1:18889
  -> browser-ai-bridge.exe --forward-proxy
  -> TLS + Basic Auth
  -> 自有美国 HTTPS 代理
  -> 目标网站
```

它用于“不经过 Chrome、由 VS Code 直接连接自有服务器”的可选场景。代理只绑定 IPv4
loopback，只允许目标端口 80/443，拒绝私有和本地目标；上游 TLS 使用系统信任库并验证
证书。凭据从 `%LOCALAPPDATA%\FanVPNBridge\direct-proxy.json` 读取，不写入仓库或日志。
上游失败时请求失败，不回退到本机公网直连。

三种模式由扩展弹窗、桌面入口或启动器显式选择。启动器只给新启动的 VS Code 进程注入代理环境，并使用
VS Code 的 Chromium 代理参数，不设置 Windows 全局代理。由于 VS Code 是单实例应用，
切换前必须关闭全部 VS Code 窗口。
