# FanVPN Bridge

[![Windows CI](https://github.com/cheguevara-great-man/fanvpn-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/cheguevara-great-man/fanvpn-bridge/actions/workflows/ci.yml)

让 Windows 开发工具借助 Chrome 中的 FanVPN 访问 OpenAI、Anthropic 和 Gemini，
不需要 Clash、系统 VPN，也不要求 FanVPN 提供本地代理端口。

> 当前项目面向 Google Chrome 116+ 和 Windows。Chrome 中需要已安装并启用
> FanVPN。默认的 `18888` 浏览器桥接不是通用 HTTP、SOCKS 或 CONNECT 代理；
> 另有一个默认关闭、需要单独配置凭据的 VS Code 直连模式。

## 工作方式

```text
Codex / Claude Code / CC Switch
              │ HTTP · 127.0.0.1:18888
              ▼
     browser-ai-bridge.exe
              │ Chrome Native Messaging
              ▼
       FanVPN AI Bridge
              │ Offscreen fetch
              ▼
        Chrome + FanVPN
              │
              ▼
 OpenAI / Anthropic / Google Gemini
```

Bridge 只负责安全、透明的 HTTP 传输。AI 协议转换不在 Bridge 中完成：例如
Claude Code 使用 Gemini 时，由 CC Switch 转换 Anthropic Messages 与 Gemini Native，
并维护 Gemini 的 `thoughtSignature`。

## 主要能力

- 单一 Native Host 同时提供本地 HTTP 网关和 Chrome 通道。
- OpenAI、ChatGPT Codex、Anthropic、Gemini Native 等显式路由。
- 流式响应、分片、按请求隔离的背压、并发上限、超时和客户端取消。
- Browser Full（浏览器完整，实验）对经过明确允许的只读账号、插件和连接器元数据使用按账号及 Token 摘要隔离的
  有界内存缓存；可重试的元数据 GET 从进入浏览器调度起最多占用 15 秒。网络失败最多重试一次，
  因交互请求让路的重启另有 4 次独立上限。
- 成功和失败的浏览器请求都会报告不含凭据的排队、fetch、尝试和抢占时序，便于区分本地调度与上游网络长尾。
- 仅监听 `127.0.0.1`，校验本地 Host/Origin，上游由静态 allowlist 限制。
- Chrome 出口不可用时失败关闭，不回退到系统直连。
- Codex 首次登录可通过一次性助手完成，无需复制其他电脑的 `auth.json`。
- Codex 提供稳定的 Browser Lean、Browser Full（浏览器完整，实验）和可选 Direct 三种模式；
  `Browser` 默认等同 Browser Lean。
- Chrome 扩展弹窗提供“服务器直连”“浏览器精简”“浏览器完整（实验）”三个按钮；关闭全部
  VS Code 后点击，Bridge 会事务式更新托管配置并按所选模式启动 VS Code。三种模式都应从按钮启动，
  不能只看上次磁盘配置后再从普通 VS Code 图标打开。
- A/B 事务式更新，切换前自动冒烟测试，失败时恢复旧注册。
- Windows 登录后自动启动 Chrome 并等待 Bridge ready。
- VS Code Claude Code 可在 Anthropic 官方模式和 Gemini 模式之间切换，且不接管全局 Claude 配置。
- 可选的 VS Code 直连模式通过本机 `18889` 连接自有 HTTPS 代理；安装后仍提供 Lean、Full、Direct
  三个桌面启动入口，作为扩展弹窗之外的备用入口。

## 快速开始

### 1. 构建 Native Host

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_native_host.ps1 `
  -Python "C:\path\to\python.exe"
```

### 2. 加载 Chrome 扩展

打开 `chrome://extensions`，启用开发者模式，加载仓库中的 `chrome-extension`
目录。在扩展详情中，将 **FanVPN AI Bridge → 网站访问权限** 设为
**在所有网站上**。

### 3. 注册和启动

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

刷新扩展后检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
```

返回 HTTP 200 且 `ready=true` 后，再按[客户端使用指南](docs/USAGE.md)配置
Codex、Claude Code 或 CC Switch。

完成 Codex 登录后，推荐完全退出 VS Code，打开 FanVPN AI Bridge 弹窗并点击“浏览器精简”或
“浏览器完整（实验）”。按钮会写入受管理的 provider 和产品端点配置，再自动启动 VS Code；
不需要手工输入三种模式的 provider。“服务器直连”只有在完成可选直连安装后才能使用。

如果还部署了配套的自有 HTTPS 代理，可按[安装文档](docs/INSTALLATION.md#可选安装-vs-code-直连模式)
安装可选直连模式，再按[客户端使用指南](docs/USAGE.md#三种-vs-code-网络模式)选择入口。
浏览器桥接仍是默认方式。

## 路由

| 用途 | 本地 Base URL |
|---|---|
| OpenAI Responses API | `http://127.0.0.1:18888/openai/v1` |
| ChatGPT Codex backend | `http://127.0.0.1:18888/chatgpt-codex` |
| ChatGPT 产品后端（仅 Browser Full） | `http://127.0.0.1:18888/chatgpt-backend` |
| VS Code Codex 界面产品接口（Browser 模式自动管理） | `http://127.0.0.1:8000/api` |
| Codex 登录 Token Exchange、刷新和注销 | `http://127.0.0.1:18888/auth-openai` |
| Anthropic Messages API | `http://127.0.0.1:18888/anthropic` |
| Gemini Native | `http://127.0.0.1:18888/gemini` |
| Gemini OpenAI compatibility | `http://127.0.0.1:18888/gemini-openai/v1` |

路由来自 `config/routes.example.json`。API Key 不应写入路由配置。

默认 `Browser` 是稳定优先的 Browser Lean，不接管 Codex Apps、插件目录和完整账号产品后端。
Browser Full（浏览器完整，实验）会把产品后端也交给 Chrome，并使用有界、短期、仅进程内存在的
只读元数据缓存。Host 或 Chrome 重启后缓存立即清空。个人 Skills、本地脚本及手工配置的本地 MCP
在 Lean 中仍可使用。具体边界见[客户端使用](docs/USAGE.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [文档导航](docs/README.md) | 从安装、使用、开发或排障开始 |
| [架构](docs/ARCHITECTURE.md) | 当前系统边界、组件和协议 |
| [Windows 安装](docs/INSTALLATION.md) | 构建、安装、更新、自动启动和卸载 |
| [客户端使用](docs/USAGE.md) | Codex、Claude Code、CC Switch 和 Gemini 模式 |
| [开发指南](docs/DEVELOPMENT.md) | 目录、测试、构建和开发约束 |
| [故障排查](docs/TROUBLESHOOTING.md) | 面向当前版本的诊断步骤 |
| [问题与解决记录](docs/PROBLEM_SOLVING.md) | 开发过程中的问题、原因和经验 |
| [用量上报客户端](docs/TOKEN_USAGE.md) | 在每台电脑启用 Token/Credits 上报，并查看本机队列和额度策略 |

## 安全边界

- 只监听 IPv4 loopback，不向局域网开放。
- 客户端不能通过 URL、query 或 header 指定任意上游。
- 自动重定向被禁止，避免请求逃逸到未配置的上游。
- 认证头只在内存中转发，诊断输出不打印 Token、Cookie 或 API Key。
- 浏览器执行器不可用时返回错误，不尝试绕过 FanVPN。
- Bridge 不读取或修改 Codex 的任务、项目、侧栏或本地数据库。
