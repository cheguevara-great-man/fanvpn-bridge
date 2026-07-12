# 需求与验收标准

## 用户目标

在不安装 Clash 等系统代理软件、不要求 FanVPN 提供本地代理端口的前提下，让本机 VS Code 中的 Codex、Claude Code 以及 CC Switch 管理的 API 请求借助 Chrome FanVPN 访问境外 AI API。

## 第一阶段范围

### 必须实现

- Windows + Google Chrome + 已可用的 FanVPN 扩展。
- 本机 loopback HTTP Base URL。
- OpenAI Responses API 的请求/流式响应透明传输。
- Anthropic Messages API 的请求/流式响应透明传输。
- Gemini OpenAI-compatible API 的请求/流式响应透明传输。
- 多个显式 route 同时存在，不用反复改一个全局 target URL。
- 大请求/响应分片、并发、取消、超时、重连和可诊断错误。
- 开发者模式安装、卸载和健康检查。

### 不在 Bridge 中实现

- Responses ↔ Chat Completions ↔ Anthropic ↔ Gemini Native 的协议转换。
- Gemini thought signature 的生成、伪造或会话存储。
- 系统级 VPN、SOCKS/HTTP CONNECT 通用代理或局域网代理。
- 浏览器登录态/Cookie 复用。
- WebSocket（第一阶段 Codex 使用 Responses SSE；WebSocket 后续单独评估）。

## 角色和场景

### 场景 A：Codex 直连 OpenAI

Codex 的自定义 provider 把 Responses API Base URL 指向本地 `openai` route。Bridge 不修改 body，Chrome 把请求发送到 OpenAI。

### 场景 B：Claude Code 直连 Anthropic

Claude Code 把 API Base URL 指向本地 `anthropic` route。Bridge 保留 `x-api-key`、`anthropic-version` 与 beta headers。

### 场景 C：Codex 经 CC Switch 使用 Gemini

CC Switch 接收 Codex/Claude 请求，转换为 Gemini 原生请求并维护 thought signature 状态；它把 Gemini 原生上游 Base URL 指向 Bridge 的 `gemini` route。只有供应商本身要求 OpenAI compatibility 格式时才使用 `gemini-openai`。Bridge 只传输转换后的 HTTP。

## 非功能要求

### 安全

- 默认且强制监听 IPv4 loopback；未来支持 IPv6 时仅允许 `::1`。
- route 目标只能来自静态配置，必须是 HTTPS（测试 fixture 例外）。
- 禁止任意转发、CONNECT、URL header 覆盖和 DNS rebinding 风格目标切换。
- 日志默认只记录 request id、route、method、path、status、字节数和耗时。
- API Key 不落盘；诊断包必须二次脱敏。

### 可靠性

- 连接中断必须有确定的超时和错误，不无限挂起。
- 在途请求按 request id 隔离。
- 对 SSE/NDJSON/普通 body 使用同一字节流机制。
- Chrome 或 FanVPN 不可用时不尝试系统直连。

### 性能基线

- 至少 8 个并发流式请求。
- 单次请求体至少支持 16 MiB。
- 单次响应体不设协议级总大小上限，采用背压避免随响应长度增长内存。
- 单个方向默认最多 4 × 256 KiB 未确认原始数据。

### 可维护性

- Host 与扩展通过版本化 JSON Schema 契约。
- 核心状态机可在没有 Chrome、FanVPN 和真实 API Key 的 CI 中测试。
- 所有平台/供应商特殊逻辑位于适配器，不渗透到协议层。

## 验收门槛

第一阶段只有在以下证据全部存在时才算完成：

1. fake-extension 集成测试覆盖非流式、SSE、大请求、大响应、并发和取消。
2. Chrome 扩展控制台与 Host 日志能关联同一 request id，且不含密钥。
3. FanVPN 开启时，出口探测和至少一个真实 AI API 请求成功。
4. FanVPN 关闭/故障时，同一请求返回 `PROXY_CONNECTION_FAILED` 或 `EGRESS_UNAVAILABLE`，没有系统直连。
5. Codex `/v1/responses` 完成一次包含工具调用的多轮任务。
6. Claude Code `/v1/messages` 完成一次流式任务。
7. CC Switch + Gemini 3 完成多轮工具调用且无 missing thought signature 400。
