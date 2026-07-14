# 实机验证状态

验证日期：2026-07-14（Asia/Shanghai）

## 已验证链路

```text
127.0.0.1 HTTP
  -> fanvpn-bridge/fanvpn-bridge.exe
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

原生 Gemini SSE 已真实返回预期文本。Gemini route 的请求头 allowlist 也已用 Claude/SDK 元数据头完成回归验证，确认不会再因 Chrome CORS 预检出现 `Failed to fetch`。

Claude Code 经 CC Switch 使用 `gemini-3.5-flash` 已完成两项真实测试：简单文本响应成功；Bash 工具调用、多轮结果回传和最终响应成功。这证明 CC Switch 的 Gemini 3+ `thoughtSignature` 保存与回放补丁在实际链路中有效。测试期间 Google 曾返回一次瞬时 503，CC Switch 自动重试后成功。

## 本机安装状态

- Native Host 注册：`HKCU\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge`
- 当前构建目录：`dist-runtime-20260713-headerfilter\fanvpn-bridge`
- 用户级 `NO_PROXY`：包含 `127.0.0.1,localhost`
- Edge：未注册

## 2026-07-14 重启故障修复

- 会话数据未丢失；主线程的 SQLite 和 rollout 文件完整，但新版侧栏状态缺少 `thread-workspace-root-hints`，形成可搜索但无法归入项目的孤儿线程。
- 已提供严格按 SQLite `cwd` 匹配的非破坏性迁移工具；不修改数据库和聊天正文，并切换为 chronological 侧栏排序。
- 新建项目任务正确写入 `fanvpn_chatgpt` provider、当前项目 cwd 和 rollout 文件。旧运行版因 Codex 模型刷新超时留下 9～10 个最长 600 秒的活动请求，浏览器连接被占用，新任务卡在首轮流式请求。
- Native Host 现会轮询本地 socket 断开，并立即向 Chrome 发送 `request.abort`；对应集成回归通过。
- 新增 `/health`、`/ready`、`/routes`，并注册登录任务自动启动 Chrome、等待 readiness 和重试。
- 修复版构建：`dist-runtime-20260714-recovery-v2\fanvpn-bridge`。
- 重启后发现 Chrome 将 Bridge 的 Manifest Host Permissions 标记为 withheld；这会让允许 CORS 的 Gemini/OpenAI 探针正常，却让 `chatgpt.com/backend-api/codex` 在 Offscreen 中表现为 `Failed to fetch`。授权 **FanVPN AI Bridge → 网站访问权限 → 在所有网站上** 后，诊断变为 `chatgpt_site_access_granted=true`，未认证 Codex 探针从 502 恢复为预期 401。
- 最终新项目任务在 5.9 秒内完成并精确回复 `FANVPN_FINAL_OK`；Bridge 保持 `ready=true`、`active_requests=0`、`last_error_code=null`。
- 两段误归档的当前项目旧会话已解除归档、补齐项目映射并置顶；验收测试会话仅归档、未删除。
- 全局恢复预览识别出 9 个目录仍存在的未归档历史项目，其中 8 个缺少精确 workspace-root 映射；新增 `--all-projects` 迁移和退出后修复助手，避免运行中的 Codex 用旧内存状态覆盖恢复结果。

## 已完成的第一阶段目标

- Codex CLI 复用现有 ChatGPT 登录，经 `chatgpt-codex` route 完成真实 `codex exec`。
- Claude Code 经 CC Switch 转换 Gemini Native，并通过 Chrome/FanVPN 完成真实流式和工具调用。
- 两条链路的外网出口均由浏览器扩展承担，不依赖 Bridge 提供 SOCKS/CONNECT 端口。

## 后续可选验证

- OpenAI Responses API authenticated SSE。
- Anthropic Messages API authenticated SSE。
- Codex VS Code 扩展在 UI 会话中长期稳定运行。
- Claude VS Code 扩展在 UI 会话中长期稳定运行。
