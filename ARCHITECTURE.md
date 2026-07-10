# FanVPN Bridge — 架构与技术文档

## 概述

FanVPN Bridge 是一个本地桥接系统，让 VS Code AI 编程插件通过 Chrome 浏览器中的 FanVPN 扩展访问境外 AI 服务（OpenAI、Gemini、Anthropic 等）。

**核心问题：** FanVPN 作为 Chrome 浏览器扩展，不开放本地代理端口，其他程序无法直接使用其通道。

**解决方案：** 通过 Chrome Native Messaging 机制，构建一个"本地程序 ↔ Chrome 扩展"的桥接链路，让 VS Code 插件的 HTTP 请求经过 Chrome 浏览器上下文发出，从而走 FanVPN 代理。

---

## 架构图

```
┌──────────────────────────────────────────────────────────┐
│  VS Code (Codex / Cline / Continue / Kilo Code 等)       │
│  Base URL = http://127.0.0.1:18888                       │
│  POST /v1/chat/completions                               │
└─────────────────────┬────────────────────────────────────┘
                      │  HTTP (普通 HTTPS 请求)
                      ▼
┌──────────────────────────────────────────────────────────┐
│  bridge.py (SERVER 模式)                                  │
│  PID 主进程，用户手动启动，常驻后台                         │
│                                                          │
│  :18888  ← HTTP Server (ThreadingHTTPServer)             │
│            · 接收 VS Code 插件的 OpenAI 兼容 API 请求       │
│            · 路径重写（strip_path_prefix）                 │
│            · 生成 request_id，加入等待队列                 │
│                                                          │
│  :18889  ← Bridge TCP Listener                          │
│            · 接受来自 bridge 进程的 TCP 连接               │
│            · 双向转发 JSON 消息帧                          │
└──────────┬───────────────────────┬───────────────────────┘
           │  TCP :18889           │  HTTP :18888
           │  (消息帧协议)           │  (请求 → 响应)
           ▼                       ▼
┌──────────────────────┐   VS Code 插件的请求从这里进入
│  bridge.py           │   响应也从这里返回
│  (BRIDGE 模式)        │
│  Chrome 拉起           │
│                       │
│  stdin/stdout  ←→  TCP :18889
│  纯消息帧转发，不解析内容
└──────────┬────────────┘
           │  Native Messaging (stdin/stdout)
           │  4 字节 LE uint32 长度 + UTF-8 JSON
           ▼
┌──────────────────────────────────────────────────────────┐
│  Chrome Extension (FanVPN AI Bridge)                      │
│  Manifest V3, Service Worker + Offscreen Document         │
│                                                          │
│  background.js (Service Worker)                          │
│    · chrome.runtime.connectNative() 建立 NM 连接          │
│    · 接收 HTTP 请求，转发给 Offscreen Document            │
│    · 25s 心跳 keep-alive，断线自动重连                     │
│                                                          │
│  offscreen.js (Offscreen Document)                       │
│    · 在页面上下文中执行 fetch()                            │
│    · 支持流式 (SSE) 和非流式响应                           │
│    · 流式响应分批发回 Service Worker                       │
└──────────┬──────────────────────────┬───────────────────┘
           │  fetch() 页面上下文       │  sendMessage
           │  (走 Chrome 代理设置)     │  (批量流式块)
           ▼                          ▼
┌──────────────────────┐   Service Worker 接收流式块
│  FanVPN 扩展          │   逐块转发给 Native Host
│  (chrome.proxy API)   │
└──────────┬───────────┘
           │  代理隧道
           ▼
┌──────────────────────────────────────────────────────────┐
│  境外 AI API                                              │
│  api.openai.com / generativelanguage.googleapis.com 等   │
└──────────────────────────────────────────────────────────┘
```

---

## 关键设计决策

### 1. 为什么需要 Offscreen Document

**问题：** Chrome MV3 Service Worker 中的 `fetch()` 不经过 `chrome.proxy` 设置的代理。FanVPN 通过 `chrome.proxy` API 设置代理，但仅对"页面上下文"生效，Service Worker 的请求绕过代理直接走系统网络栈。

**解决：** 使用 `chrome.offscreen` API 创建一个隐藏的 Offscreen Document（拥有 DOM 的页面上下文），所有 `fetch()` 在此执行，FanVPN 即可正常拦截。

```
Service Worker (接收 NM 消息)
    │
    │ chrome.runtime.sendMessage({type: "fetch-request", ...})
    ▼
Offscreen Document (页面上下文)
    │
    │ fetch(url, options)  ← 走 chrome.proxy → FanVPN
    ▼
   境外 AI API
```

### 2. 为什么用 TCP 而不是单进程

**问题：** Chrome Native Messaging 要求 Chrome 拉起宿主进程，通过 stdin/stdout 通信。如果宿主进程同时要跑 HTTP Server，当 Service Worker 断开重连时，Chrome 会拉起第二个进程，导致端口冲突。

**解决：** 二进程架构。同一份 `bridge.py` 自动判断角色：

- **首次启动**（用户手动）：`:18888` 可绑定 → **SERVER 模式**，启动 HTTP Server + Bridge Listener
- **Chrome 拉起**：`:18888` 已占用（connect 成功）→ **BRIDGE 模式**，连接 TCP，纯转发

```python
def main():
    # 尝试连接 HTTP 端口来判断角色
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", config["port"]))
        run_bridge(config)   # 已连接 → BRIDGE
    except (ConnectionRefusedError, OSError):
        run_server(config)   # 无法连接 → SERVER
```

### 3. 为什么用 ThreadingHTTPServer 而不是 HTTPServer

**问题：** Python 内置 `HTTPServer` 是单线程的。当 AI API 请求阻塞等待响应时（尤其是流式长连接），所有后续请求都会被阻塞。

**解决：** 改用 `http.server.ThreadingHTTPServer`，每个请求独立线程处理。

---

## 消息协议

### 帧格式（TCP + Native Messaging 统一使用）

```
┌──────────────────────────────────────┐
│  4 bytes: uint32 LE = 消息体长度      │
├──────────────────────────────────────┤
│  N bytes: UTF-8 JSON                 │
└──────────────────────────────────────┘
最大消息: 16 MiB
```

### 请求消息 (SERVER → BRIDGE → Extension)

```json
{
  "id": "a1b2c3d4",
  "method": "POST",
  "url": "https://api.openai.com/v1/chat/completions",
  "headers": {
    "Authorization": "Bearer sk-xxx",
    "Content-Type": "application/json"
  },
  "body": "<base64 编码的请求体>"
}
```

### 响应消息 (Extension → BRIDGE → SERVER)

**非流式完成：**
```json
{
  "id": "a1b2c3d4",
  "type": "complete",
  "status": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "<base64>"
}
```

**流式块：**
```json
{"id": "a1b2c3d4", "type": "stream", "status": 200, "headers": {...}}
{"id": "a1b2c3d4", "type": "stream", "body": "<base64 chunk>"}
{"id": "a1b2c3d4", "type": "stream", "body": "<base64 chunk>"}
{"id": "a1b2c3d4", "type": "done"}
```

**错误：**
```json
{"id": "a1b2c3d4", "type": "error", "error": "Failed to fetch"}
```

**心跳：**
```json
{"type": "ping"}
{"type": "pong"}
```

---

## 请求生命周期

```
1. VS Code 插件发送 POST http://127.0.0.1:18888/v1/chat/completions
2. ProxyHandler._proxy() 接收请求
3. 应用 strip_path_prefix 配置：/v1/chat/completions → /chat/completions
4. 拼接完整 URL：target_base_url + 路径
5. 移除 hop-by-hop headers (Host, Connection 等)
6. BridgeManager.forward_request() 生成 request_id
7. 将请求 JSON 通过 TCP :18889 发送给 BRIDGE 进程
8. BRIDGE 进程通过 stdout 转发给 Chrome 扩展
9. Chrome Service Worker 接收，转发给 Offscreen Document
10. Offscreen Document 执行 fetch()（走 FanVPN 代理）
11. 响应沿原路返回：fetch → sendMessage → NM → TCP → PendingRequest
12. PendingRequest 逐块传递响应给 HTTP ResponseWriter
13. 流式响应使用 chunked transfer encoding 实时转发
14. 请求完成，清理 pending request
```

---

## 并发处理

```
SERVER 主线程
  ├── HTTP Server (ThreadingHTTPServer)
  │   ├── 请求线程 1: 等待 bridge 响应...
  │   ├── 请求线程 2: 等待 bridge 响应...
  │   └── 请求线程 3: 等待 bridge 响应...
  │
  ├── Bridge Accept Thread: 接受 TCP 连接
  │   └── Bridge Handler Thread: 读 TCP → dispatch to pending[]
  │
  └── 共享状态
      ├── self.bridge_conn (conn_lock)
      ├── self.pending = {id: PendingRequest} (pending_lock)
      └── self.write_lock (TCP 写同步)

BRIDGE 进程
  ├── stdin→TCP thread: 读 stdin → 写 TCP
  └── TCP→stdout thread: 读 TCP → 写 stdout
```

---

## 配置 (config.json)

```json
{
    "port": 18888,
    "bridge_port": 18889,
    "target_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "strip_path_prefix": "/v1",
    "request_timeout": 120
}
```

| 字段 | 说明 |
|------|------|
| `port` | HTTP Server 监听端口 |
| `bridge_port` | Bridge TCP 监听端口 |
| `target_base_url` | 目标 API 基础 URL |
| `strip_path_prefix` | 从请求路径中剥离的前缀 |
| `request_timeout` | 请求超时秒数 |

### 配置示例

| 目标 API | target_base_url | strip_path_prefix |
|----------|----------------|-------------------|
| OpenAI | `https://api.openai.com` | `""` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `"/v1"` |
| Anthropic | `https://api.anthropic.com` | `""` |
| Groq | `https://api.groq.com/openai` | `"/v1"` |
| OpenRouter | `https://openrouter.ai/api` | `""` |

### 路径重写示例 (Gemini)

```
VS Code 插件请求:  POST /v1/chat/completions
strip_path_prefix: "/v1"
重写后路径:         /chat/completions
最终 URL:           https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
```

---

## 项目结构

```
fanvpn-bridge/
├── native-host/
│   ├── bridge.py              # 核心桥接程序 (SERVER + BRIDGE 二合一)
│   ├── bridge.bat             # Native Messaging 启动器 (Chrome 调用)
│   ├── config.json            # 配置文件
│   └── test_bridge.py         # 端到端测试脚本
├── chrome-extension/
│   ├── manifest.json          # MV3 扩展声明
│   ├── background.js          # Service Worker (NM 连接 + 消息路由)
│   ├── offscreen.html         # Offscreen Document 宿主页
│   └── offscreen.js           # fetch() 执行器 (页面上下文)
├── install.ps1                # Windows 安装脚本
└── ARCHITECTURE.md            # 本文档
```

---

## 安装与使用

### 前置条件

- Python 3.8+
- Google Chrome（已安装 FanVPN 扩展）
- VS Code + AI 编程插件

### 安装步骤

```powershell
# 1. 加载 Chrome 扩展
# Chrome → chrome://extensions → 开发者模式 → 加载已解压的扩展
# 选择: fanvpn-bridge\chrome-extension
# 记下 32 位扩展 ID

# 2. 注册 Native Messaging
cd D:\software\Note\fanvpn-bridge
.\install.ps1 -ExtensionId "你的扩展ID"

# 3. 刷新 Chrome 扩展
# chrome://extensions → FanVPN AI Bridge → 🔄 刷新

# 4. 编辑 config.json 设置目标 API
# native-host\config.json

# 5. 启动桥接服务
python D:\software\Note\fanvpn-bridge\native-host\bridge.py
```

### VS Code 插件配置

插件 Base URL 设为：

```
http://127.0.0.1:18888
```

API Key 填写目标服务的真实 Key。模型名填写目标服务支持的模型。

---

## 已测试验证

| 目标 API | 非流式 | 流式 SSE | 通过 |
|----------|--------|----------|------|
| OpenAI (`api.openai.com`) | ✅ 401 (无效 key) | — | FanVPN 通道 |
| OpenRouter (`openrouter.ai`) | ✅ 200 + 模型列表 | — | 完整链路 |
| Gemini (`generativelanguage.googleapis.com`) | ✅ 200 + "Hello" | 🔄 测试中 | 完整链路 |

### Gemini 测试结果

```
GET /v1/models → 200 OK, 55 models
POST /v1/chat/completions (非流式) → 200 OK
{
  "choices": [{
    "message": {"content": "Hello", "role": "assistant"}
  }],
  "model": "gemini-2.5-flash"
}
```

---

## 故障排查

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| `502 Bridge not connected` | Chrome 未运行或扩展未加载 | 检查 chrome://extensions 确认扩展已启用 |
| 请求超时无响应 | Offscreen Document 未创建 | 刷新扩展，检查扩展错误日志 |
| `Connection refused` | bridge.py 未启动 | 运行 `python bridge.py` |
| 重复进程 | 旧版 bug (SO_REUSEADDR) | 已修复，使用新版 bridge.py |
| FanVPN 不生效 | fetch 在 Service Worker 执行 | 已修复，改用 Offscreen Document |

---

## 技术限制

- **不支持 WebSocket：** 仅支持标准 HTTP 请求（GET/POST/PUT/DELETE）
- **Chrome 必须运行：** bridge 依赖 Chrome 扩展作为网络出口
- **MV3 限制：** Service Worker 可能被 Chrome 回收（有心跳保活，但不能 100% 保证）
- **单文件 > 1 MiB：** 受 Native Messaging 消息大小限制，超大型请求/响应可能失败
- **仅 Windows：** 当前安装脚本仅支持 Windows（代码本身跨平台）
