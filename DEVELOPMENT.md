# 开发说明

## 已完成的 v2 切片

- 严格 JSON 配置、loopback 绑定和 HTTPS route allowlist。
- Native Messaging 4-byte framing、1 MiB Host 出站保护、256 KiB body 分片。
- `hello/hello_ack` 版本协商、累计 ACK、最多 4 个未确认 frame。
- 并发 request id 状态机、取消、超时和稳定错误码。
- HTTP/1.1 Gateway、健康/版本端点、chunked 流式响应。
- Offscreen fetch 执行器；不提供 Service Worker 直连 fallback。
- 固定开发扩展 ID、独立 EXE 构建、Chrome HKCU Native Host 安装/卸载。
- 无真实 API Key 的 unit、fake-extension integration 和 packaged-EXE smoke tests。

## 包结构

```text
native-host/fanvpn_bridge/
├─ config.py          # 严格配置与 allowlist
├─ contracts.py       # 稳定端口/领域对象
├─ errors.py          # 跨层错误码
├─ framing.py         # Chrome Native Messaging framing
├─ protocol.py        # envelope、分片、base64、FlowWindow
├─ routing.py         # 本地 route -> HTTPS upstream
├─ dispatcher.py      # 并发请求状态机
├─ http_server.py     # loopback HTTP/1.1 gateway
└─ main.py            # Native Host 组合根

chrome-extension/src/
├─ protocol.js        # 浏览器端契约
├─ background.js      # Native Port、握手、重连、Offscreen 生命周期
├─ offscreen.js       # 请求组装、fetch、响应流和背压
└─ popup.js           # 本机诊断状态
```

## 测试命令

项目运行时只依赖 Python 标准库。开发测试使用 Python 3.12+ 与 Node 22+：

```powershell
python tools\run_v2_tests.py
node tools\check_extension.mjs
node tools\check_extension_identity.mjs
node tools\check_offscreen.mjs
python tools\smoke_native_exe.py
```

目前覆盖：配置拒绝、开放代理防护、路径重写、framing、分片、ACK、2 MiB request、四路并发、认证头、HTTP health、chunked response 与独立 EXE 生命周期。

## 下一实现顺序

1. ✅ 在真实 Chrome 中验证 Offscreen 请求经过 FanVPN。
2. ✅ 增加无凭据 `POST /__bridge/probe/{route}` 出口探测。
3. 使用真实 OpenAI/Anthropic/Gemini Key 做原样 HTTP/SSE 验证。
4. 配置 Codex 与 Claude Code 的 Base URL。
5. 回到 `cc-switch` 仓库完成 Gemini 3 多轮工具调用和 CI 产物验证。
6. 增加发布 ZIP、版本升级和诊断日志脱敏。

## 已知限制

- 本地 HTTP 请求目前要求 `Content-Length`，不接收 chunked request body。
- Offscreen Fetch API 会自动解压 HTTP body，因此 Bridge 删除上游 `Content-Encoding` 和 `Content-Length`，再以 chunked 编码返回。
- 浏览器侧请求体在 fetch 前缓冲，当前上限 32 MiB；响应体不整体缓存。
- WebSocket 尚未实现，第一阶段以 Responses/Anthropic SSE 为目标。
- Offscreen API 没有专用于网络出口的 reason；当前使用 `DOM_SCRAPING` 是开发模式兼容方案，未来需评估更合适的 Chrome 执行上下文。

## CC Switch 边界

`cheguevara-great-man/cc-switch` 是独立交付物。Gemini `thoughtSignature` 的捕获、part 顺序和后续回放必须在该仓库修复，不能下沉到 Bridge 传输层。
