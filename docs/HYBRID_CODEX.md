# Codex Hybrid：同时使用 GPT 与 Gemini

Hybrid 模式让 **Codex 始终担任 Agent**，并在同一个 Codex 模型菜单中同时提供 GPT 与 Google 账号
Gemini。选择 GPT 时复用现有 ChatGPT 登录链路；选择 Gemini 时由 Bridge 转换模型推理协议。Shell、文件编辑、
MCP、Skills、计划、工具循环和子 Agent 编排始终由 Codex 负责。

## 使用方法

1. 先按 [Gemini 账号说明](GEMINI_ACCOUNT.md#第一次使用)完成一次 Google 登录。
2. 完全关闭所有 VS Code 窗口。
3. 打开 **Browser AI Bridge** 扩展，把“Codex 模型模式”选为“GPT + Gemini + WebGPT”，再选择所需的原生 GPT 请求链路和子 Agent 策略。
4. 点击“应用配置并启动 VS Code”，然后直接在 Codex 原生模型菜单选择 GPT、Gemini 或 WebGPT。

Hybrid 继续使用原来的 `browser_ai_bridge` Provider 标识，因此不会为了增加 Gemini 而人为切断已有 GPT
任务的本地分区。原生 GPT 推理可在统一 Provider 内独立选择官方直连、浏览器完整或服务器中心；
ChatGPT 产品后端仍走固定的 Browser Full 链路。Gemini 任务以本地使用为主，不承诺同步到 ChatGPT 云端。

## 三种子 Agent 策略

| 插件按钮 | 实际行为 | 适合场景 |
|---|---|---|
| 子 Agent 固定 Gemini | 主 Agent 仍自由选择；Codex 发出的可见协作和审查子 Agent请求被 Bridge 固定为 Gemini 3.7 Flash High | 希望稳定节省 OpenAI 额度 |
| 子 Agent 默认 Gemini | 写入 Codex 官方 `[agents]` 默认值，默认为 Gemini 3.7 Flash High；Codex 或用户显式指定的模型仍可覆盖 | 希望 GPT/Gemini 混合调度 |
| Codex 原生决策 | 不设置或改写任何子 Agent模型，完全由 Codex 原生规则决定 | 追求官方默认行为 |

“固定”模式不是把默认值包装成强制。当前 Codex 会为子 Agent 模型请求发送
`x-openai-subagent`；Bridge 只在值为 `collab_spawn` 或 `review` 时改写模型。主 Agent没有该标记，绝不会
被改写。`compact` 和 `memory_consolidation` 等内部维护请求会保留 GPT 路径，因为它们承担历史压缩或记忆
维护，不是普通可替换的协作 Agent。

这个识别字段来自 Codex 自身实现；如果未来 Codex 删除该字段，Bridge 会安全地不执行强制，而不会凭请求
内容猜测身份。[OpenAI 官方子 Agent 文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)
说明了默认模型、显式启动参数和角色文件之间的优先级。

## 配置默认值和角色

选择“子 Agent 默认 Gemini”后，展开插件中的“配置默认 Gemini 与自定义角色”：

- 默认模型和强度都用下拉菜单选择，菜单来自当前合并后的 GPT/Gemini 目录；
- 可以新增、删除角色，为每个角色选择模型和强度，并填写用途描述与角色指令；
- Bridge 角色只写入 `~/.codex/agents/browser-ai-bridge-*.toml`，不会改动用户已有的其他角色文件；
- 切换到另外两种策略时，Bridge 会恢复进入配置模式前的 `[agents]` 默认值，并把 Bridge 创建的角色改为
  非 TOML 后缀使其停用；下次回到配置模式会重新启用，不会影响用户自己维护的角色。

Codex 的解析优先级是：角色文件中固定的模型/强度，其次是启动子 Agent 时显式指定的值，再其次是
`[agents]` 默认值，最后继承主 Agent。因而“默认 Gemini”有意不覆盖 Codex 对某一次任务的明确选择。

## 请求路由与上下文

Hybrid Provider 使用 `http://127.0.0.1:18888/hybrid/v1`：

- 请求模型以 `gemini-` 开头：交给 Google 账号推理适配器；
- 其他原生 GPT 模型：按 `hybrid-route.json` 中的固定枚举交给官方直连、`chatgpt-codex` 浏览器链路或本机服务器中心客户端；
- `/responses/compact` 等产品生命周期接口始终交给 ChatGPT Codex；
- 请求不能提供任意上游地址，Hybrid 不是开放代理。

把 GPT 历史送给 Gemini 时，Bridge保留可见的用户/助手消息、工具调用与工具结果，忽略 OpenAI 专用的
加密推理项；把 Gemini 回合留给后续 GPT 时，Codex仍能看到可见消息和工具结果。Bridge不能也不需要解密
模型隐藏思考过程。

## 回退

弹窗仅保留新的四层配置；Browser Lean 只为旧脚本兼容保留。配置失败时，模式切换事务会恢复修改前的
`config.toml`、模型目录、VS Code 设置、路由状态和子 Agent 策略文件。
