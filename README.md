# FanVPN Bridge

FanVPN Bridge 让本机 Codex、Claude Code、CC Switch 等开发工具，通过 Chrome 中的 FanVPN 扩展访问境外 AI API，而不需要 Clash、系统 VPN 或 FanVPN 本地代理端口。

## 当前实现

```text
Codex / Claude Code / CC Switch
              │ HTTP on 127.0.0.1:18888
              ▼
      fanvpn-bridge.exe
              │ Chrome Native Messaging（分片 + 背压）
              ▼
     FanVPN AI Bridge 扩展
              │ Offscreen fetch（失败关闭）
              ▼
          FanVPN 扩展
              │
              ▼
 OpenAI / Anthropic / Gemini 等预设 HTTPS 上游
```

Bridge 只转发 HTTP 字节，不把 Responses API、Chat Completions、Anthropic Messages 或 Gemini Native 相互转换。Gemini 3 `thoughtSignature` 等会话语义继续由 CC Switch 负责。

## 实机状态（2026-07-13）

- Chrome + FanVPN 页面出口：通过。
- 独立 Native Host + 固定 ID 扩展握手：通过。
- `GET /__bridge/health`：`status=ok`、`executor=offscreen`。
- 无凭据出口探测：OpenAI 401、Anthropic 401、Gemini/Google 404；三者均收到真实境外 API 响应。
- Gemini API Key 鉴权、Gemini Native SSE：通过。
- Codex 使用现有 ChatGPT 登录执行真实请求：通过。
- Claude Code → CC Switch → Gemini 3.5 的真实多轮工具调用及 `thoughtSignature` 回放：通过。
- Gemini 浏览器请求头 allowlist：通过，已消除 Claude/SDK 元数据头导致的 CORS 预检失败。
- Windows 登录启动、Bridge readiness 和 Codex 项目会话映射恢复：已实现。
- 本地客户端断开后会在 250 ms 轮询周期内取消浏览器请求，避免重启恢复阶段积累 600 秒僵尸请求。

## Windows 快速安装

完整说明见 [Windows 安装与诊断](docs/INSTALL_WINDOWS.md)。开发机最短流程：

1. 构建独立 Native Host：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 -Python "C:\path\to\python.exe"
   ```

2. Chrome 打开 `chrome://extensions`，启用开发者模式，加载目录 `chrome-extension`。
3. 注册 Native Host（固定扩展 ID 已内置）：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
   ```

4. 刷新扩展，点击工具栏图标，确认 Native Host、协议握手和 `offscreen` 均正常。
5. 检查本地网关：

   ```powershell
   Invoke-RestMethod http://127.0.0.1:18888/__bridge/health
   ```

安装脚本还会创建当前用户的 `FanVPN Bridge Bootstrap` 登录任务：后台启动 Chrome、修复 Codex 项目映射，并等待 Bridge ready。详见 [Windows 重启恢复与 Codex 会话修复](docs/RECOVERY_WINDOWS.md)。

## 本地 Base URL

| 客户端/目标 | Base URL |
|---|---|
| Codex → OpenAI | `http://127.0.0.1:18888/openai/v1` |
| Codex → ChatGPT subscription backend | `http://127.0.0.1:18888/chatgpt-codex` |
| Claude Code → Anthropic | `http://127.0.0.1:18888/anthropic` |
| CC Switch → Gemini native | `http://127.0.0.1:18888/gemini` |
| CC Switch → Gemini OpenAI compatibility | `http://127.0.0.1:18888/gemini-openai/v1` |

路由定义位于打包目录的 `routes.json`，源模板是 [config/routes.example.json](config/routes.example.json)。配置不保存 API Key；客户端发送的认证头只在内存中转发。

Codex、Claude Code 和 CC Switch 的具体配置见 [docs/CLIENT_INTEGRATION.md](docs/CLIENT_INTEGRATION.md)。

## 验证

```powershell
# Python 核心、fake extension 与 HTTP Gateway
python .\tools\run_v2_tests.py

# 浏览器协议和 Offscreen 执行器
node .\tools\check_extension.mjs
node .\tools\check_offscreen.mjs

# 已打包 EXE 的 Native Messaging + health smoke test
python .\tools\smoke_native_exe.py
```

## 文档

- [需求与验收标准](docs/REQUIREMENTS.md)
- [目标架构](ARCHITECTURE.md)
- [开发说明](DEVELOPMENT.md)
- [Windows 安装与诊断](docs/INSTALL_WINDOWS.md)
- [实机验证状态](docs/STATUS.md)
- [Windows 重启恢复与 Codex 会话修复](docs/RECOVERY_WINDOWS.md)
- [Native Messaging v1 契约](contracts/native-messaging-v1.schema.json)

## 安全边界

- 强制监听 `127.0.0.1`，不向局域网开放。
- 只允许配置中的 HTTPS 上游，不接受客户端提供任意目标主机。
- 浏览器执行器不可用时失败关闭，不回退到系统直连。
- Host→Chrome 单条消息受 1 MiB 保护，body 使用 256 KiB 分片。
- 浏览器侧请求体最多缓冲 32 MiB；响应体按流和 ACK 背压传输。
