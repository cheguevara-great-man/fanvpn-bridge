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

## 组件

### Native Host

`native-host/fanvpn_bridge/` 包含：

- `config.py`：严格解析监听、协议和路由配置。
- `routing.py`：将本地 route 映射到固定 HTTPS 上游。
- `framing.py`：Chrome Native Messaging 的 4-byte length framing。
- `protocol.py`：版本化 envelope、分片、Base64 和流控。
- `dispatcher.py`：按 request id 隔离并发请求、超时和取消。
- `http_server.py`：loopback HTTP/1.1 网关与诊断端点。
- `runtime_logging.py`：脱敏、轮转的本地运行日志。
- `codex_login.py`：一次性 OAuth PKCE 登录、loopback 回调、凭据备份和原子写入。
- `main.py`：组合根和进程生命周期。

### Chrome 扩展

`chrome-extension/src/` 包含：

- `background.js`：Native Port、握手、重连和 Offscreen 生命周期。
- `offscreen.js`：组装请求、执行 fetch、处理取消和回传响应。
- `stream.js`：立即转发响应 chunk，并用独立空 frame 表示结束。
- `protocol.js`：浏览器端协议常量和校验。
- `popup.js`：连接、握手、执行器和站点权限状态。

## 本地 HTTP 接口

客户端使用 `http://127.0.0.1:18888/{route}`。`route` 必须存在于运行时
`routes.json`，客户端不能提供任意上游 URL。

`chatgpt-codex` 承载登录后的 Codex 模型请求和模型目录。Browser 精简模式只接管这条模型数据通道，
同时在 Codex 配置中关闭 Apps、插件同步、远程插件目录和分析请求。`auth-openai` 承载 Token
Exchange、刷新和注销；普通 API 请求不会进入该路由。

账号产品后端属于独立的控制面，包括插件安装状态、Apps/连接器、任务元数据和账号信息。
2.2.1 曾尝试用单一 `chatgpt-backend` 路由整体转发该控制面，但真实环境出现并发失败和重试风暴，
因此 2.2.2 不再暴露或默认使用该路由。通用 route/dispatcher 架构仍然保留，后续可以在明确目标域名、
请求头、响应协议和失败隔离后，按接口族逐项增加受控路由。

首次登录使用独立的一次性助手：助手按 Codex 官方 OAuth 参数生成 PKCE 和 `state`，
打开 Chrome 官方授权页，在 `127.0.0.1` 临时端口接收授权码，然后只把 Token Exchange
请求送入 `auth-openai`。成功后凭据按 Codex 当前格式原子写入 `~/.codex/auth.json`；
旧文件先备份。Native Host 的正常网关模式不会读取或保存该文件，日志也不记录授权码
或 Token。

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
- Offscreen 不可用时失败关闭，不回退到 Service Worker 或系统直连。
- 日志不记录请求正文、认证头、Token、Cookie 或 API Key。
- 请求完成日志只记录 route、method、HTTP 状态和响应头/首段数据/结束耗时，不记录 URL 或 query。

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

两种模式由启动器显式选择。启动器只给新启动的 VS Code 进程注入代理环境，并使用
VS Code 的 Chromium 代理参数，不设置 Windows 全局代理。由于 VS Code 是单实例应用，
切换前必须关闭全部 VS Code 窗口。
