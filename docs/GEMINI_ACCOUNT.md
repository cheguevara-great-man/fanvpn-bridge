# 在 Codex 中使用 Gemini 账号模型

这个模式实现的是：**Codex 继续担任 Agent，Gemini 只提供模型推理**。

因此，文件读写、终端命令、MCP、Skills、计划与工具循环仍由 Codex 执行；正常对话不会启动
Antigravity Agent，也不需要 Gemini API Key。模型额度来自已登录的 Google 账号（例如 Google AI Pro）。

## 工作链路

```text
VS Code Codex
  -> OpenAI Responses 协议
  -> 127.0.0.1:18888/gemini-account/v1
  -> Bridge 协议适配器
  -> Chrome 浏览器网络
  -> Google Code Assist 账号模型
```

Bridge 把 Codex 的消息、工具声明和工具结果转换为 Google 账号模型协议，再把文本流和工具调用转换回
Responses 事件。Codex 收到工具调用后仍由自己执行工具，并把结果送入下一轮模型请求。

## 第一次使用

1. Chrome 中同时启用可访问 Google 的浏览器代理扩展和 **Browser AI Bridge**。
2. 至少登录一次 Google 账号。在 PowerShell 中运行：

   ```powershell
   & "$env:LOCALAPPDATA\agy\bin\agy-browser.exe"
   ```

   如果已经登录过并且 Antigravity CLI 能正常对话，不需要重新登录。
3. 完全关闭所有 VS Code 窗口。
4. 打开 **Browser AI Bridge** 扩展弹窗，点击 **Codex + Gemini 账号**。
5. 等待 VS Code 自动打开，然后在 Codex 面板正常新建任务。

也可以在仓库目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\tools\start_vscode_network_mode.ps1 -Mode GeminiAccount
```

## 登录与凭据

- Google OAuth 凭据由官方 CLI 保存在 Windows 凭据管理器的 `gemini:antigravity` 项中。
- Bridge 只在发起 Google 请求时读取它，不复制到项目文件、`config.toml` 或日志。
- Access Token 临近过期时，Bridge 会静默调用官方 CLI 的登录刷新能力；它只负责刷新登录，不参与 Agent 回合。
- Refresh Token 失效时，需要重新在 PowerShell 中运行一次 `agy-browser.exe` 完成登录。

## 模型与强度

Bridge 在每次点击“Codex + Gemini 账号”时，从当前 Google 账号读取真实可用模型并重新生成 Codex
模型目录。因此 Google 后续增加新模型时，无需升级 Bridge 就会自动出现在菜单中。Google 返回的
`*-tiered` 是供客户端按推理强度分档的内部入口；菜单会把它显示为普通模型名，例如
`gemini-3.7-flash-tiered` 显示为 Gemini 3.7 Flash，并被自动选为默认模型。同一家族同时返回的
High、Medium、Low 别名会自动合并，避免模型名称和 Codex 推理强度重复。

所有支持的系列都使用同一套两级菜单：Model 只显示模型家族，Reasoning 再显示该家族真实支持的
强度。Gemini 3.7、3.6 和 3.5 Flash 提供 Light、Medium、High；Gemini 3.1 Pro 提供 Light、High。
Bridge 会把选择映射到 Google 账号目录中的正确内部型号，而不只是修改菜单文字。

仅用于图片生成或 Google 自身 Agent 的型号不会加入 Codex 推理菜单。官方目录临时读取失败时，会继续使用
上一次成功取得的目录；从未成功读取过时才使用随程序附带的兼容备用目录。

生成目录会复用当前 Codex 自带模型目录中的 Agent 指令和工具元数据，因此 Shell、文件编辑、MCP 和 Skills
仍由 Codex 驱动。切回其他模式时，脚本会精确恢复原来的 `model` 与 `model_catalog_json` 设置，不影响
OpenAI 或 CC Switch 的模型目录。如果客户端仍请求非 Gemini 模型，Provider 也会安全地映射到默认模型。

注意：Codex 目前会用 OpenAI 登录状态决定是否显示自定义模型菜单，因此 Gemini 模式仍要求 Codex 中保留
有效的 OpenAI 登录；OpenAI Token 只会发到本机 `127.0.0.1` 的 Bridge，不会转发给 Google。实际推理和
额度消耗仍来自 Google 账号。

Codex 的 Light、Medium、High 会分别映射为 Gemini 的 `low`、`medium`、`high`，不提供 Minimal。
Google 可能调整账号可用模型、额度和命名，
因此模型目录以当前账号实时返回为准。

## 与其他模式的关系

如果希望 GPT 与 Gemini 同时出现在一个 Codex 模型菜单，并让主 Agent 与子 Agent分别选择模型，请使用
[Codex Hybrid](HYBRID_CODEX.md)。“仅 Gemini”模式保留为隔离测试和兼容回退入口。

| 模式 | Agent | 模型与额度 | 网络出口 |
|---|---|---|---|
| 浏览器精简 / 完整 | Codex | OpenAI 登录账号 | Chrome 浏览器链路 |
| Codex + Gemini 账号 | Codex | Google 登录账号 | Chrome 浏览器链路 |
| Antigravity CLI | Antigravity | Google 登录账号 | Chrome 浏览器链路 |

切换模式不会删除 OpenAI 的 `auth.json` 或 Google 的 Windows 登录凭据。以后切回“浏览器精简”或
“浏览器完整”，仍可继续使用 OpenAI 登录账号。

## 当前边界

- 仅支持 Windows，因为当前登录凭据来自 Windows 凭据管理器。
- 依赖 Google 当前未公开稳定承诺的 Code Assist 内部接口；Google 升级协议后可能需要同步适配。
- 账号资格、地区限制、模型开放范围和额度由 Google 决定，Bridge 不绕过这些限制。
- 该模式默认关闭远程 Apps、远程插件目录和 analytics，避免向 Google 模型模式混入只属于 ChatGPT
  产品后端的控制面请求；本地 Shell、MCP、Skills 和 Codex 工具仍可使用。

## 验证

检查 Bridge 与模型目录：

```powershell
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
(Invoke-RestMethod http://127.0.0.1:18888/gemini-account/v1/models -Proxy $null).data.id
```

如果模型目录返回 401，请重新登录 Google；如果无法连接 `18888`，请刷新 Browser AI Bridge 并确认
Chrome 仍在运行。
