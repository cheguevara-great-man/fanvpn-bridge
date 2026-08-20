# Codex 服务器执行器总体方案

> 状态：第一阶段 Server Lite 已开始实现；尚未部署到生产服务器
> 开发分支：两个仓库均使用 `codex/server-executor`  
> 目标：账号 B 只在美国服务器保存登录凭据，五台 Windows 电脑通过各自设备身份使用服务器上的 Codex 模型能力；第二阶段再实现 Full 账号产品能力。  
> 非目标：新分支不兼容 Browser Lean、Browser Full、Direct、Gemini、Hybrid 或 Antigravity；旧方案继续保留在原 `master` / `main` 分支。

## 1. 结论

采用两个可独立交付的阶段：

1. **Server Lite**：先完成模型目录、Responses 流式对话、压缩、Token 统计和设备额度控制。
2. **Server Full**：在 Lite 的稳定传输上增加 Apps、插件、连接器、账号 MCP 和其他 ChatGPT 产品接口。

新分支只有一条服务器链路。`Server Lite` 和 `Server Full` 是同一套客户端与服务器的两个能力等级，
不是与旧 Browser 模式并存的网络模式。首发 Lite；实现 Full 后由配置开关启用额外产品接口，模型通道不变。

旧浏览器方案不在这个分支里承担兼容、回滚或回归要求。需要旧方案时切回仓库原分支，而不是在运行时保留
两套进程、按钮和端口。

服务器方案不是把整个 Agent 搬到服务器。Codex 的文件读取、代码修改、Shell、Git、Skills、本地 MCP
和子 Agent 仍在各台 Windows 电脑执行；服务器只负责账号 B 的模型请求和第二阶段的账号产品请求。

## 2. 为什么分两步

Lite 与 Full 的共同部分是：

- 设备认证；
- TLS 公网入口；
- 服务器账号凭据与刷新；
- 流式 HTTP 转发；
- 并发、取消、超时和错误映射；
- 服务器端用量统计与设备额度控制。

Full 额外涉及内部产品接口、不同方法和路径、Apps/MCP、OAuth 连接状态、缓存和未来接口变化。
先完成 Lite 可以较早验证最关键的三个假设：

1. VS Code Codex 能否在客户端没有账号 B 凭据时使用自定义 Server Provider；
2. 服务器能否可靠刷新账号 B 并流式转发 Codex Responses；
3. 五台设备能否被独立识别、统计、限额和停用。

这三个假设成立后，Full 只是扩展同一条认证传输，不需要重做底层。

## 3. 总体拓扑

### 3.1 Server Lite

```text
VS Code Codex / Codex CLI
  -> http://127.0.0.1:18890/v1/codex
  -> Browser AI Bridge Server Client（独立后台进程，不依赖 Chrome）
  -> HTTPS + 设备 Bearer Token
  -> https://美国服务器:9444/v1/codex/*
  -> Codex Executor
  -> 服务器账号 B 的 Authorization + ChatGPT-Account-ID
  -> https://chatgpt.com/backend-api/codex/*
```

本机只保存设备 Token，不保存账号 B 的 access token、refresh token 或 Cookie。Lite 的回环端口仅绑定
`127.0.0.1`；它不向局域网开放，且固定转发路径，不是通用代理。

### 3.2 Server Full

```text
Codex app-server 产品请求
  -> http://127.0.0.1:18888/product/backend-api/*
  -> Server Client
  -> https://美国服务器:9443/v1/product/backend-api/*
  -> Product Executor
  -> ChatGPT 产品后端

VS Code Codex WebView
  -> http://localhost:8000/api/*
  -> Server Client 的受限 8000 兼容入口
  -> 同一个 Product Executor
```

Server Full 复用旧 Browser Full 中已经验证过的产品路径、认证补齐、MCP 探测和缓存规则，但运行时不启动
Native Messaging 或 Chrome Offscreen Document。

## 4. 两个仓库的职责

### 4.1 `fanvpn-bridge`：Windows 客户端

负责：

- 向 Codex 提供稳定的 loopback HTTP 接口；
- 从现有设备配置读取服务器地址和设备 Token；
- 把请求安全地转发给服务器；
- 后台进程生命周期、诊断、安装和升级；
- Server Full 时提供固定的 `localhost:8000/api` 兼容入口。

不负责：

- 保存账号 B 的 OpenAI 登录凭据；
- 直接向 ChatGPT 发出 Server 模式请求；
- 在 Server 模式重复统计 Token；
- 在本机决定服务器端设备是否被禁用。

### 4.2 `browser-gateway`：Linux 服务器

负责：

- Nginx TLS 入口和限速；
- 设备 Token 验证与启用状态；
- 账号 B 凭据保存、刷新和健康状态；
- 固定上游、固定路径的 HTTP/SSE 转发；
- 服务器端权威用量统计和额度拦截；
- Server Full 的产品路径策略、产品缓存和 Apps/MCP 请求。

服务器现有 GOST、sing-box、Chrome 正向代理和用量网页继续独立运行。Codex Executor 是新增服务，不能把
账号 B 的认证逻辑塞进 GOST 或通用正向代理。第一阶段使用独立 TCP `9444` 与独立 systemd 服务，不重启或
改写现有代理、用量网页、Nginx 配置或其端口 `9443`。

## 5. 本机进程设计

### 5.1 独立 Server Client

把 `browser-ai-bridge.exe` 的默认运行形态改为独立 Windows 后台客户端，例如：

```text
browser-ai-bridge.exe --server-client --config <固定配置路径>
```

它与 Native Messaging Host 共用 HTTP 解析、日志、usage 解析等可复用代码，但生命周期完全独立：

- 默认监听 `127.0.0.1:18890`，避免占用旧浏览器 Bridge 的 `18888`；
- Server Full 时额外监听 `127.0.0.1:8000`；
- Chrome 退出不会导致它退出；
- 使用 PID/状态文件防止重复启动；
- Windows 登录任务负责启动，安装器负责健康检查和升级；
- 只允许固定 Server 路径，不提供 CONNECT 或任意目标 URL。

测试期新 Server Client 不运行旧 Chrome Native Host，但仍固定使用 `18890`，让旧浏览器 Bridge 能继续在
`18888` 并行运行。稳定后再决定是否迁移端口或停止旧链路。新 Server Client 不提供 `18889` Direct
Forward Proxy。

### 5.2 本地认证与边界

设备 Token 保存在受当前 Windows 用户 ACL 保护的运行目录，只由 Server Client 用于服务器认证；它不写入
Codex TOML，也不转发给 VS Code。Codex 自定义 Provider 无法稳定注入另一枚专用的 loopback Bearer，
因此 Lite 回环入口依赖 Windows 用户边界：只监听 `127.0.0.1`、不接受任意上游 URL、只允许固定的 Responses
路径。它不是可供其他机器使用的服务。

### 5.3 Codex Provider

Server Lite 计划生成托管 Provider：

```toml
model_provider = "server_codex_executor"

[model_providers.server_codex_executor]
name = "Server-side Codex Executor"
base_url = "http://127.0.0.1:18890/v1/codex"
requires_openai_auth = false
wire_api = "responses"
supports_websockets = false
```

实现后必须做一个最小验证：确认当前 VS Code Codex 能以该无本地 OpenAI 凭据的 Provider 正常进入聊天。
如果当前扩展仍强制要求本地登录，应先解决这个兼容层，不能把账号 B 复制到五台客户端作为正式方案。

Server Full 额外设置：

```toml
chatgpt_base_url = "http://127.0.0.1:18888/product/backend-api/"
```

并把 VS Code 的 `chatgpt.apiEndpoint` 设置为 `localhost` 开发入口。Lite 不启用产品入口；Full 启用后保持
该配置，不需要在多个运行模式之间反复改写和恢复。

## 6. 设备注册与配置

沿用 Browser Gateway 已有的一次性注册码和设备 Token，不再建立第二套机器身份。

设备兑换响应增加：

```json
{
  "codexExecutorUrl": "https://服务器:9444/v1/codex"
}
```

Gateway 扩展同步给 Bridge 的配置增加：

```json
{
  "executor_url": "https://服务器:9444/v1/codex",
  "device_token": "设备 Token"
}
```

服务器数据库仍只保存设备 Token 的 SHA-256。`devices.enabled=false` 应同时禁止：

- 新的模型请求；
- Full 产品请求；
- 用量上报和只读网页登录。

第一版所有已启用设备默认拥有 `codex:invoke` 权限，符合“所有电脑权限相同”的当前产品要求。数据结构中
保留 `scopes` 扩展位，但第一阶段不在 UI 暴露复杂权限编辑。

## 7. Server Lite 接口

公网只暴露固定前缀：

| 方法 | 公网路径 | 固定上游 |
|---|---|---|
| GET | `/v1/codex/models` | `/backend-api/codex/models` |
| POST | `/v1/codex/responses` | `/backend-api/codex/responses` |
| POST | `/v1/codex/responses/compact` | `/backend-api/codex/responses/compact` |
| GET | `/v1/executor/capabilities` | 本地生成，不访问上游 |
| GET | `/v1/executor/health` | 本地生成，需要设备认证 |

如果实际 Codex 日志证明还使用 response retrieve、cancel 或 delete，再按“方法 + 路径”逐项加入。不能直接
开放任意 `/backend-api/*`。

请求规则：

- 客户端的 `Authorization`、`Cookie`、`ChatGPT-Account-ID` 一律不转发；
- 服务器用账号 B 的 access token 和 account ID 重建上游认证头；
- 删除 hop-by-hop header；
- 不自动跟随跨主机重定向；
- 请求体上限沿用 32 MiB；
- SSE 响应边读取边发送，Nginx 禁止 buffering；
- 客户端断开时取消上游读取；
- POST 在请求体已经发送后不做透明自动重试，避免重复任务；
- 上游正常 4xx/5xx 原样保留，服务器自身错误使用稳定错误码。

建议的本地错误：

| HTTP | code | 含义 |
|---:|---|---|
| 401 | `invalid_device_token` | 设备身份无效 |
| 403 | `device_disabled` | 管理员停用了设备 |
| 429 | `device_quota_exceeded` | 设备达到额度上限 |
| 503 | `server_account_unavailable` | 账号 B 需要重新登录或刷新失败 |
| 502 | `upstream_connection_failed` | 服务器未取得有效上游响应头 |
| 504 | `upstream_timeout` | 上游超时 |

账号 B 的上游 401 不应直接变成客户端的 401，否则 Codex 会误以为本机 API Key 错误。服务器刷新失败后应
返回 `503 server_account_unavailable`，并在管理网页显示“服务器账号需要重新登录”。

## 8. 账号 B 的凭据生命周期

### 8.1 第一阶段配置方式

先提供管理员脚本，把单独临时目录中生成的账号 B `auth.json` 通过 SSH 上传到服务器。脚本必须：

- 校验 `auth_mode=chatgpt` 以及 access/refresh token 和 account ID；
- 不在命令行、日志或进程参数中打印 Token；
- 通过临时文件上传并在服务器原子替换；
- 文件权限设为 `browser-gateway:browser-gateway 0600`；
- 由服务账户在需要时原子刷新，临时文件同样受其目录 ACL 限制；
- 上传完成后调用只返回脱敏状态的健康检查。

正式文件路径为：`/var/lib/browser-gateway/codex-auth.json`。

```text
/etc/browser-gateway/codex-auth.json
```

后续可再增加服务器设备码登录或管理网页登录，但这不是 Lite 首发的必要条件。

### 8.2 刷新策略

新增 `ServerCredentialStore`：

- access token 临近过期时提前刷新；
- 多线程刷新使用 single-flight 锁，同一时刻只刷新一次；
- 刷新成功后原子更新凭据文件；
- refresh token 轮换时必须保存新值；
- 管理状态只显示账号 ID 摘要、过期时间和最后刷新结果；
- 模型 POST 遇到 401 时不盲目重放；先标记账号不可用，下一请求在刷新成功后恢复。

服务器是账号 B refresh token 的唯一正式持有者。不要在五台电脑保留同一份 refresh token。

## 9. 用量统计和额度控制

Server 模式的 Token 统计以服务器为唯一权威来源：

1. Server Client 根据设备 Token 被服务器识别为具体 `machine_id`；
2. Codex Executor 在转发 SSE 的同时读取 usage 事件；
3. 直接调用现有 Usage Collector 的内部接口或复用其数据库函数；
4. 事件中的机器 ID 由服务器认证结果生成，不信任客户端 header 或正文；
5. 本机 Bridge 对 `server_*` 路由不再重复上报，防止双计数。

在发出上游请求前检查设备策略。设备停用必须 fail-closed；统计服务短暂不可用时，额度策略是否 fail-open
由配置决定，首发建议 fail-open 并记录告警，避免统计页面故障导致五台机器全部无法工作。

Server Lite 验收必须确认：同一请求只记一次、模型名称正确、压缩请求继承原模型、缓存 Token 正确、设备停用
立即生效。

## 10. 并发和性能

目标规模是五台电脑，不需要分布式系统。建议：

- 全局最多 16 个在途模型请求；
- 每台设备最多 4 个在途模型请求；
- 每台设备单独限速，防止一台机器占满全部连接；
- 上游连接池按 `chatgpt.com` 复用连接；
- SSE 首段不缓存、不压缩、不等待完整响应；
- Nginx 设置 `proxy_buffering off`、`proxy_request_buffering off`；
- 模型请求读超时至少 600 秒，连接超时 10 秒；
- 记录 `server_queue_ms`、`upstream_head_ms`、`first_body_ms`、`total_ms`，不记录正文。

实现语言建议继续使用 Python，但为稳定的异步 SSE、连接池和取消使用独立、锁定版本的 `aiohttp` 运行环境；
不要在 1900 行的 `usage_collector.py` 中继续堆代理代码。若后续模型选择标准库实现，必须补齐流式、取消、
连接复用和并发测试，不能退化为整包缓冲。

## 11. Server Full 设计

### 11.1 先做接口盘点

在修改 Full 之前，用现有 Browser Full 的安全诊断收集一次真实启动和一个只读 App 调用，只记录：

- method；
- path 和 query 参数名；
- status；
- content-type；
- 耗时和重试次数。

不记录 Token、Cookie、header 值、请求正文、用户输入和响应正文。以真实记录建立 Full 路径契约，不能凭猜测
开放整个 ChatGPT 后端。

### 11.2 产品接口策略

Server Full 的公网前缀：

```text
/v1/product/backend-api/*
```

服务器按“路径模式 + HTTP 方法 + 请求体上限”执行 allowlist。首批范围：

- 插件目录、推荐列表、已安装状态；
- Apps/连接器目录和账号连接状态；
- `/backend-api/ps/mcp` 的正式 POST；
- `/backend-api/wham/apps` 及经实测需要的 `wham` 工具调用；
- Codex 当前启动确实需要的账号和任务元数据 GET。

OAuth metadata GET、只支持 POST 的 MCP 探测等继续沿用现有 Browser Full 的本地快速响应。任何未知路径默认拒绝，
并只记录脱敏的 method/path，供后续版本显式加入。

### 11.3 产品缓存

把现有 `product_cache.py` 的规则迁到服务器，并保留相同安全边界：

- 只缓存明确 allowlist 的认证 JSON GET；
- 不缓存 POST、MCP、工具调用、模型请求或带 `Set-Cookie` 的响应；
- cache key 包含账号摘要、完整 URL 和影响结果的请求头摘要；
- 不保存 Token 和 Cookie；
- 相同 key 的并发 miss 合并；
- 插件安装状态等短 TTL 数据在修改请求后主动失效。

服务器缓存可以被五台电脑共享，比每台本地缓存更有效。

### 11.4 Apps 的实际能力边界

Full 不重新实现 GitHub、Drive 等 App 的业务逻辑。它负责把 Codex 产品请求正确送到账号 B 的 ChatGPT 产品后端。
实际工具仍由 OpenAI 的 App/远程 MCP 和外部服务执行。

首发至少选择一个已经在账号 B 连接好的只读 App 做端到端验收：

1. 目录中可见；
2. 安装/连接状态正确；
3. Codex 能选择工具；
4. 工具实际读取一项非敏感数据；
5. 结果回到模型并完成回答。

“目录加载成功”不能作为 Full 完成标准。App 新建 OAuth 连接、交互式授权和写操作分开验收。

### 11.5 Full 不承诺的内容

- 不保证不同套餐、地区或工作区未授权的 App 可用；
- 不绕过账号 B 自身权限；
- 不把五台电脑的本地历史数据库合并为一个数据库；
- 不保证未观察到的未来 WebSocket 产品接口自动兼容；
- 不把普通网页 Chat 的聊天额度转换成 Codex 额度。

## 12. 安全边界

- 公网服务只接受 HTTPS；
- 设备 Token 只存哈希，禁用设备立即失效；
- 账号 B 凭据只在服务器受限文件中保存；
- 客户端无法指定上游 host、scheme、port 或任意 URL；
- 客户端 Authorization、Cookie、Account-ID 永不透传到上游；
- 服务器不记录提示词、回复正文、工具参数、文件、Cookie 或 Token；
- Full 未知路径默认拒绝；
- 管理网页账号与设备 Token 分离，设备不能获得管理员权限；
- systemd 服务继续使用 `NoNewPrivileges`、`ProtectSystem=strict` 和最小可写目录；
- 账号不可用、设备停用和上游故障必须使用不同错误码，便于诊断。

## 13. 建议代码布局

### 13.1 `browser-gateway`

```text
server/
  codex_executor.py          # HTTPS 后端、流式转发和路由入口
  codex_credentials.py       # 账号 B 读取、刷新和原子保存
  codex_policy.py            # 固定上游、方法/路径 allowlist
  codex_usage.py             # SSE usage 解析和中央记账适配
  codex_protocol.py          # 错误结构、版本和 header 规则
  test_codex_executor.py
  test_codex_credentials.py
  test_codex_policy.py
```

部署增加：

- `browser-gateway-codex.service`；
- loopback 端口 `19444`；
- Nginx `/v1/codex/`、`/v1/executor/`，Full 阶段再加 `/v1/product/`；
- `/etc/browser-gateway/codex-auth.json`；
- 管理员凭据上传/更新脚本；
- 健康检查与回滚。

### 13.2 `fanvpn-bridge`

```text
native-host/fanvpn_bridge/
  server_client.py           # 18888/8000 loopback 服务
  server_transport.py        # 固定服务器 HTTPS 客户端
  server_client_config.py    # ACL 配置和本地 Token
  server_protocol.py         # 服务器协议与错误映射

tools/
  start_server_client.ps1
  configure_server_executor.ps1
  diagnose_server_mode.ps1
```

现有文件需要小范围扩展：

- 删除运行时网络模式切换依赖；Lite/Full 只作为服务器能力开关；
- 安装器一次性写入 Server Provider 和 Full 所需的产品端点；
- `DeviceConfigController`：保存 executor URL 和设备 Token；
- 不依赖 Chrome 扩展按钮；使用本机诊断页或命令显示服务器、设备、账号和 Full 状态；
- `usage_reporting.py`：Server 路由不进行本地重复上报。

## 14. 实施顺序

### 阶段 0：契约验证，不写完整功能

1. 用假的 loopback Responses 服务验证 VS Code Codex 的 `env_key` Provider 在无账号 B 本地登录时可启动。
2. 从现有 `chatgpt-codex` 日志确认 Lite 实际使用的方法和路径。
3. 固定客户端—服务器协议版本和错误结构。
4. 为两个仓库建立测试 fixture，不连接真实 OpenAI。

通过条件：VS Code 能进入聊天页并向假服务发出 `/models` 和 `/responses`。

### 阶段 1A：Server Lite 最小纵向切片

1. 服务器读取一份测试账号凭据；
2. 单设备 Token 认证；
3. `/models` 和 `/responses` 流式转发；
4. 本机 Server Client 18888；
5. PowerShell 手工启动模式；
6. 不做 UI、不做 Full。

通过条件：没有安装或启动 Chrome Bridge 扩展时，VS Code 能连续完成三轮对话，SSE 流式正常。

### 阶段 1B：Lite 产品化

1. 凭据刷新 single-flight；
2. 五设备注册、停用和限额；
3. 服务器权威 Token 统计；
4. 并发、取消、超时、断线恢复；
5. 自动后台启动、自动安装和升级；
6. 诊断、文档和服务器部署。

通过条件：五台设备标识正确；同一事件不重复计数；不依赖 Chrome；服务器服务重启后自动恢复。

### 阶段 2A：Full 只读控制面

1. 真实 Browser Full 接口盘点；
2. 插件/Apps/连接器目录；
3. 已安装和账号连接状态；
4. 产品缓存迁移；
5. 未知路径拒绝与诊断。

### 阶段 2B：Full 工具调用

1. MCP 正式 POST；
2. `wham` Apps 工具链；
3. 一个只读 App 端到端调用；
4. OAuth 重新连接流程；
5. 按 App 补兼容测试。

## 15. 测试矩阵

### 单元测试

- 设备 Token 哈希匹配、停用和机器 ID 不可伪造；
- 固定路径 allowlist，编码、双斜杠、重定向和路径穿越拒绝；
- 客户端 Authorization/Cookie/Account-ID 被替换；
- access token 过期、刷新轮换、并发只刷新一次；
- POST 不被危险重放；
- usage 只记一次且设备归属由认证决定；
- 所有日志脱敏。

### 集成测试（假上游）

- SSE chunk 立即到达客户端；
- 客户端取消能关闭上游；
- 五设备并发互不串流；
- Nginx 不缓冲；
- 上游 4xx/5xx 与本地错误可区分；
- Server Full 未知产品路径被拒绝；
- 缓存命中、TTL、失效和 single-flight。

### 真实验收

- Server Lite：模型列表、首条消息、连续消息、压缩、长任务、图片输入；
- 没有 Chrome Bridge 和 Gateway 浏览器代理仍能使用；
- 设备停用后新请求被拒绝，重新启用后恢复；
- Token 网页按设备正确增加；
- Full：目录、安装状态、一个只读 App 的真实工具调用；
- Lite 升级为 Full 后模型链路和本地历史不发生变化。

## 16. 性能目标

相对于服务器直接访问 ChatGPT，上述两层本地转发的额外目标开销：

- 本机 Server Client：响应头额外中位延迟小于 10 ms；
- Nginx + Executor：响应头额外中位延迟小于 30 ms；
- 不增加等待完整响应的缓冲；
- 空闲内存：Server Client 小于 80 MiB，Executor 小于 200 MiB；
- 五设备、每设备两个并发流时无串流和明显排队。

性能测试必须把 OpenAI 模型生成时间和本项目增加的代理时间分别记录，不能用总回答时间代替 Bridge 开销。

## 17. 分支与故障恢复

- 两个仓库的旧浏览器实现只保留在原 `master` / `main` 分支，新实现只提交到
  `codex/server-executor`；
- 两个仓库分别保留部署前备份；
- Server Client 独占 `18888`，安装时先检查并停止旧 Host，不同时运行两套链路；
- 安装器备份 Codex TOML 和 VS Code settings，卸载时可恢复；
- 服务器新增独立 systemd service，停用它不影响 GOST、sing-box 和用量网页；
- Nginx 新 location 删除后现有 9443 统计接口继续工作；
- 账号 B 凭据更新失败时保留上一份有效服务器凭据。

## 18. 后续编码模型的工作规则

1. 先完成阶段 0 和 1A，不允许同时开始 Full。
2. 每次只实现一个纵向能力，并同时增加测试。
3. 不为兼容旧 Browser/Gemini/Hybrid 引入双路由、双端口或模式切换；需要参考的代码应提取后改造成
   Server 实现，旧行为由原分支保存。
4. 不把任意 URL、命令或路径配置能力暴露给浏览器扩展。
5. 不在测试 fixture、日志、提交或异常中放真实 Token、Cookie、账号 ID 和服务器密码。
6. 真实 OpenAI 测试必须显式开启，默认测试只使用本地假上游。
7. 每个阶段完成后先给出：提交、测试结果、已知限制和回滚命令，再进入下一阶段。

## 19. 最终完成标准

Server Lite 完成必须同时满足：

- 账号 B 只在服务器保存；
- 五台设备不保存账号 B refresh token；
- 不依赖 Chrome、Native Messaging 或浏览器代理；
- 流式、取消、压缩、图片和长任务工作；
- 用量按设备准确统计并能停用；
- 部署与回滚可重复。

Server Full 完成还必须满足：

- 目录与安装状态可用；
- Apps/MCP 正式协议成功；
- 至少一个账号 B 已连接的只读 App 完成真实工具调用；
- 未知产品路径默认拒绝；
- OAuth/套餐/地区和未来接口限制在文档中如实标明。
