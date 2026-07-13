# 实机验证状态

验证日期：2026-07-12（Asia/Shanghai）

## 已验证链路

```text
127.0.0.1 HTTP
  -> dist-final/fanvpn-bridge/fanvpn-bridge.exe
  -> Chrome Native Messaging
  -> FanVPN AI Bridge v2 (bgpbajocpomglgdffkgcklhepbcfpbfd)
  -> Offscreen fetch
  -> FanVPN
  -> 境外 API
```

健康状态：

```json
{
  "status": "ok",
  "host_version": "0.2.0-dev",
  "protocol_version": 1,
  "native_channel_connected": true,
  "executor": "offscreen",
  "active_requests": 0,
  "last_error_code": null
}
```

无凭据探测结果：

| Route | 上游响应 | 结论 |
|---|---|---|
| `openai` | 401 Missing bearer authentication | 已到达 OpenAI/Cloudflare SJC |
| `anthropic` | 401 x-api-key header is required | 已到达 Anthropic/Cloudflare SJC |
| `gemini-openai` | 404 Requested entity was not found | 已到达 Google API；探测路径不需要是有效模型调用 |

原生 `gemini` route 已使用本机进程级 `GEMINI_API_KEY` 完成真实鉴权，返回 50 个模型，其中包括 Gemini 3、3.1 和 3.5 系列。Key 未写入配置、仓库或测试输出。

`chatgpt-codex` route 已使用现有 ChatGPT 登录完成两次真实 `codex exec`：HTTP fallback 和 `supports_websockets = false` 模式均成功，后者无 WebSocket 重试延迟。

这些状态码证明网络和 HTTP 透明转发成立，不代表 API 鉴权或模型调用已经通过。

## 本机安装状态

- Native Host 注册：`HKCU\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge`
- 当前构建目录：`dist-final\fanvpn-bridge`
- 用户级 `NO_PROXY`：包含 `127.0.0.1,localhost`
- Edge：未注册

## 仍需验证

- OpenAI Responses API authenticated SSE。
- Anthropic Messages API authenticated SSE。
- Codex VS Code 扩展使用本地 provider。
- Claude Code 使用本地 Anthropic Base URL。
- CC Switch + Gemini 3 多轮工具调用和 thought signature 回放。
