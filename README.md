# FanVPN Bridge

FanVPN Bridge 的目标是让只能使用浏览器代理的网络环境，也能为本机的 Codex、Claude Code、CC Switch 等开发工具提供受控的 AI API 出口。

当前仓库处于 **v2 架构奠基阶段**。已有的 `native-host/bridge.py` 与 `chrome-extension/*.js` 是可供验证假设的 v1 原型；它们尚未满足大请求分片、严格失败关闭、稳定生命周期和完整可观测性要求，不应视为可发布版本。

## 目标链路

```text
Codex / Claude Code / CC Switch
              │ HTTP on 127.0.0.1
              ▼
       Native Host Gateway
              │ Chrome Native Messaging
              ▼
        FanVPN Bridge Extension
              │ browser fetch
              ▼
       FanVPN extension / proxy
              │
              ▼
 OpenAI / Anthropic / Gemini / other allowlisted APIs
```

桥接器只传输 HTTP，不负责把 Responses API、Chat Completions、Anthropic Messages 或 Gemini `generateContent` 相互转换。协议转换继续由 Codex 客户端、Claude Code 或 CC Switch 负责。

## 文档入口

- [需求与验收标准](docs/REQUIREMENTS.md)
- [v2 目标架构](ARCHITECTURE.md)
- [开发阶段与仓库约定](DEVELOPMENT.md)
- [Native Messaging v1 契约](contracts/native-messaging-v1.schema.json)
- [路由配置示例](config/routes.example.json)

## 当前状态

- 已确认核心方向在 Chrome Native Messaging 能力范围内可行。
- 已确认 Gemini 3 工具调用的 `thought_signature` 是 CC Switch 的协议状态问题，不属于网络桥接层。
- 已发现 v1 原型的关键结构风险，详见架构文档。
- 当前实机 Chrome 的 FanVPN 出口处于 `ERR_PROXY_CONNECTION_FAILED`，因此尚未声明真实端到端通过。

## 安全边界

- 只监听 `127.0.0.1`，不对局域网开放。
- 只允许配置文件中声明的上游域名，不接受客户端提供任意目标 URL。
- 不保存 API Key；认证头仅在内存中转发，并从日志中脱敏。
- 浏览器出口不可用时失败关闭，不自动退回系统直连。
