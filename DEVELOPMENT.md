# FanVPN Bridge + CC Switch 补丁 — 完整开发文档

## 概述

本项目包含两个子系统：

1. **FanVPN Bridge** — 本地桥接系统，让 VS Code AI 插件通过 Chrome 浏览器中的 FanVPN 扩展访问境外 AI 服务
2. **CC Switch 补丁** — 修复 CC Switch（开源 VS Code AI 路由插件）在 Gemini 工具调用转换中丢失 `thought_signature` 的 bug

---

## 一、FanVPN Bridge

### 1.1 问题背景

- 公司电脑无法直接访问 `api.openai.com` 等境外 AI 服务
- FanVPN 是 Chrome 浏览器扩展，只代理浏览器流量，不开放本地端口
- 需要在 VS Code 中使用 Codex/Cline 等 AI 插件，它们需要直接访问 API

### 1.2 架构

```
VS Code 插件 (Codex)
    ↓ HTTP → http://127.0.0.1:18888
Python bridge.py (SERVER 模式)
    ↓ TCP :18889
Python bridge.py (BRIDGE 模式，Chrome 通过 Native Messaging 启动)
    ↓ stdin/stdout Native Messaging
Chrome Extension Service Worker (background.js)
    ↓ chrome.runtime.sendMessage
Chrome Extension Offscreen Document (offscreen.js)
    ↓ fetch()（页面上下文，走 Chrome 代理设置）
FanVPN 代理
    ↓
境外 AI API
```

### 1.3 为什么需要 Offscreen Document

Chrome MV3 Service Worker 中的 `fetch()` **不经过** `chrome.proxy` 设置的代理。FanVPN 通过 `chrome.proxy` API 代理流量，但仅对"页面上下文"生效。使用 `chrome.offscreen` API 创建隐藏页面，在其中执行 fetch()，FanVPN 即可正常拦截。

这是我们实际测试中遇到的第一个关键问题——最初用 Service Worker 直接 fetch，api.openai.com 永远超时，加 Offscreen Document 后立即通了。

### 1.4 进程架构

同一份 `bridge.py` 根据端口占用自动判断角色：

- **首次启动**（用户手动运行）：`:18888` 无人占用 → SERVER 模式
- **Chrome 通过 NM 启动**：`:18888` 已占用 → BRIDGE 模式，通过 TCP 连接 SERVER

判断方式：尝试 `connect()` 到 `127.0.0.1:18888`，连接成功=BRIDGE，连接失败=SERVER。

> **重要教训**：最初使用 `bind()+SO_REUSEADDR` 判断，在 Windows 上 `SO_REUSEADDR` 允许多进程绑定同一端口，导致多个 SERVER 同时运行。已改为 connect 方式。

### 1.5 HTTP 服务器

- 使用 `http.server.ThreadingHTTPServer`（**注意**：不能用普通 `HTTPServer`，它是单线程的，AI 流式请求会阻塞所有后续请求）
- 每个请求独立线程处理
- 支持流式 SSE（Server-Sent Events）通过 chunked transfer encoding 透明转发

### 1.6 消息协议

4 字节 LE uint32 长度前缀 + UTF-8 JSON。TCP 和 Native Messaging 统一使用。

请求：`{id, method, url, headers, body?}`
响应：`{id, type:"complete"|"stream"|"done"|"error", status?, headers?, body?}`
心跳：`{type:"ping"}` / `{type:"pong"}`

### 1.7 目录结构

```
fanvpn-bridge/
├── native-host/
│   ├── bridge.py              # 核心桥接程序（SERVER + BRIDGE 二合一）
│   ├── bridge.bat             # Native Messaging 启动器
│   ├── config.json            # 配置（端口、目标 URL、路径前缀）
│   ├── test_bridge.py         # 端到端测试
│   └── verify.py              # 系统化验证
├── chrome-extension/
│   ├── manifest.json          # MV3 清单（nativeMessaging + offscreen 权限）
│   ├── background.js          # Service Worker
│   ├── offscreen.html         # Offscreen Document 宿主
│   └── offscreen.js           # fetch() 执行器
├── install.ps1                # Windows 安装脚本
├── ARCHITECTURE.md            # 架构文档
└── DEVELOPMENT.md             # 本文档
```

### 1.8 config.json 配置

```json
{
    "port": 18888,
    "bridge_port": 18889,
    "target_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "strip_path_prefix": "/v1",
    "request_timeout": 120
}
```

`strip_path_prefix` 将 VS Code 发来的 `/v1/chat/completions` 转成 `/chat/completions`（Gemini OpenAI 兼容端点的路径格式）。

### 1.9 已验证通过的测试

| 测试 | 状态 |
|------|------|
| `/health` 诊断端点 | ✅ |
| `GET /v1/models` (55 个 Gemini 模型) | ✅ |
| `POST /v1/chat/completions` 非流式 | ✅ |
| `POST /v1/chat/completions` 流式 SSE | ✅ |
| 多线程并发请求 | ✅ |

### 1.10 流式转发的 Bug 修复记录

最初流式测试一直超时。根因：

1. Offscreen Document 的 `handleStream()` 读完流后返回 `stream-init`
2. Service Worker 的 `forwardToNative()` 收到 `stream-init` 后**直接 return，没有发 `done`**
3. 导致 bridge server 的 `PendingRequest` 永远收不到结束信号，HTTP handler 阻塞到超时

修复：`forwardToNative` 收到 `stream-init` 后调用 `safePostNative({id, type:"done"})`

### 1.11 使用方式

1. Chrome 中加载 `fanvpn-bridge/chrome-extension/`（开发者模式 → 加载已解压的扩展）
2. 运行安装脚本注册 Native Messaging：`.\install.ps1 -ExtensionId "扩展ID"`
3. 启动 bridge server：`python native-host/bridge.py`
4. VS Code 插件 Base URL 设为 `http://127.0.0.1:18888`

---

## 二、CC Switch thought_signature 修复

### 2.1 问题描述

CC Switch 在 Route Codex → Gemini 时，多轮工具调用后出现：

```
HTTP 400: Function call is missing a thought_signature in functionCall parts.
Additional data, function call `default_api:get_goal`
```

Gemini 要求在后续请求中，每个 `functionCall` 的 **part 级别**（不是 functionCall 对象内部）附带原始的 `thoughtSignature`。CC Switch 没有正确保存和恢复这个签名。

### 2.2 根因分析（两个 Bug）

**Bug 1 — transform_gemini.rs (line ~674)**

`thoughtSignature` 被放进了 `functionCall` 对象**内部**：

```json
// ❌ CC Switch 旧代码输出
{"functionCall": {"name": "...", "thoughtSignature": "abc"}}
```

但 Gemini API 要求 `thoughtSignature` 在 **part 级别**，与 `functionCall` 并列：

```json
// ✅ Gemini 期望
{"functionCall": {"name": "..."}, "thoughtSignature": "abc"}
```

修复位置：`convert_message_content_to_parts()` 函数中，将签名从 `function_call["thoughtSignature"]` 移到 `part["thoughtSignature"]`。

**Bug 2 — streaming_gemini.rs (line ~177)**

流式响应中，某些 Gemini 中继把缺失的签名序列化为 `"thoughtSignature": ""`。旧代码只检查 `is_none()`：

```rust
if tool_call.thought_signature.is_none() {
    tool_call.thought_signature.clone_from(...);
}
```

空字符串 `Some("")` 绕过了检查，覆盖了之前 chunk 中捕获的有效签名。

修复：增加空字符串判断，把空串也视为"缺失"。

### 2.3 修改的文件

1. `src-tauri/src/proxy/providers/transform_gemini.rs` — Bug 1
2. `src-tauri/src/proxy/providers/streaming_gemini.rs` — Bug 2

### 2.4 新增测试

7 个测试覆盖了：非流式签名放置位置、流式 chunk 签名保留、连续工具调用签名隔离、并行工具调用、同名工具连续调用、流式重试、模型名不影响 shadow key。

### 2.5 编译尝试过程

1. 本地编译失败：公司电脑无 Visual Studio Build Tools，`link.exe` 是 Git 带的假货；GNU 工具链缺 `dlltool.exe`；尝试 `rust-lld` 也因缺 Windows SDK libraries 失败
2. 切换到 GitHub Actions：创建私有仓库 `cheguevara-great-man/cc-switch`，push 到 `fix/thought-signature-v2` 分支

### 2.6 GitHub Actions 编译状态

- 分支：`fix/thought-signature-v2`（基于 v3.16.5 tag）
- Workflow 文件：`.github/workflows/build-patched.yml`
- **当前状态：编译成功但签名失败**

编译输出显示 EXE 生成成功：
```
Finished `release` profile [optimized] target(s) in 21m 21s
Built application at: D:\a\cc-switch\cc-switch\src-tauri\target\release\cc-switch.exe
```

但最后一步签名检查失败：
```
A public key has been found, but no private key.
Make sure to set `TAURI_SIGNING_PRIVATE_KEY` environment variable.
```

`pnpm tauri build` 默认会尝试签名，但 CI 环境没有私钥。需要跳过签名或添加 `--bundles none` 参数来只编译 EXE 不打包安装器。

### 2.7 需要完成的步骤

1. 修改 `build-patched.yml`，将 `pnpm tauri build` 改为跳过签名：
   - 设置环境变量 `TAURI_SIGNING_PRIVATE_KEY` 和 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` 为空
   - 或者用 `cargo build --release` 单独编译 Rust 部分（但需要先 `pnpm install` 构建前端）
   - 或者在 tauri.conf.json 中禁用签名
2. 重新运行 CI
3. 下载 cc-switch.exe artifact
4. 替换到 `D:\software\CC-Switch-v3.16.5-Windows-Portable\`（先备份原版）
5. 端到端测试：Codex → CC Switch → FanVPN Bridge → Gemini，确认不再出现 thought_signature 错误

### 2.8 关键路径

| 项目 | 路径 |
|------|------|
| CC Switch 源码 | `D:\software\CC-Switch-src\` |
| CC Switch 安装目录 | `D:\software\CC-Switch-v3.16.5-Windows-Portable\` |
| 原版备份 | `cc-switch-original.exe` |
| Patch 文件 | `D:\software\Note\thought-signature-fix.patch` |
| Git 分支 (main) | `fix/thought-signature` |
| Git 分支 (v3.16.5) | `fix/thought-signature-v2` |
| GitHub 仓库 | `https://github.com/cheguevara-great-man/cc-switch` |
| CI Workflow | `.github/workflows/build-patched.yml` |

### 2.9 数据库版本问题

- v3.16.5 数据库版本 = v11
- main 分支数据库版本 = v12
- patched 版本如果基于 main 编译，运行后会把数据库升级到 v12，原版 v3.16.5 无法再打开
- **必须基于 v3.16.5 tag 编译**，否则需要恢复数据库备份
- 数据库备份路径：`C:\Users\J03366\.cc-switch\backups\`

---

## 三、整体数据流（Codex 完整请求路径）

```
Codex (VS Code 插件)
  │  POST /v1/responses (Responses API 格式)
  ▼
CC Switch (本地路由)
  │  开启 Needs Local Routing + Codex 接管
  │  转换：Responses API ↔ Chat Completions API
  │  转换：tool_use ↔ functionCall
  │  保存/恢复：thought_signature (shadow store)
  │  发送：POST /v1/chat/completions
  ▼
FanVPN Bridge (bridge.py SERVER :18888)
  │  strip_path_prefix: /v1 → 空
  │  拼接目标 URL
  │  透明转发 HTTP + SSE
  ▼
Chrome Extension (Native Messaging + Offscreen Document)
  │  fetch() 在页面上下文执行
  ▼
FanVPN 代理 (chrome.proxy)
  ▼
Gemini API (generativelanguage.googleapis.com/v1beta/openai)
```
