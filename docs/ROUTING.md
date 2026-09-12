# Codex 路由与 Provider 速查

这页只解决一个问题：**弹窗里的几个“模式”分别控制什么，以及哪些组合会落到同一个 Codex `model_provider`。**

先记住两个结论：

1. **Codex 模型模式**决定模型目录里有什么；**Codex `model_provider`**决定任务属于哪个 Provider 分区。
2. **Hybrid + 官方直连**仍然是 `browser_ai_bridge`。它只是让 Hybrid 内部的原生 GPT 请求走 Direct 出口，
   不会把整个 Codex 任务切成 `browser_ai_direct`。

## 四个独立维度

| 维度 | 典型选项 | 控制范围 |
|---|---|---|
| VS Code 通用网络 | 系统网络 / 美国服务器 | 整个新启动 VS Code 的通用网络环境 |
| Codex 模型模式 | 仅原生 GPT / GPT + Gemini + WebGPT | 模型目录与统一路由能力 |
| 原生 GPT 请求链路 | 官方直连 / 浏览器完整 / 服务器中心 | 只控制原生 GPT 推理请求的出口 |
| 子 Agent 策略 | 固定 Gemini / 默认 Gemini / Codex 原生决策 | 只控制统一模型模式下的子 Agent 选模策略 |

这四层不是同一个“模式”的不同名字，不能互相替代。

## 最容易混淆的组合

| Codex 模型模式 | 原生 GPT 请求链路 | Codex `model_provider` | GPT 实际出口 | Provider 分区 |
|---|---|---|---|---|
| 仅原生 GPT | 官方直连 | `browser_ai_direct` | Direct | Direct 独立分区 |
| 仅原生 GPT | 浏览器完整 | `browser_ai_bridge` | Browser Full | Bridge 分区 |
| 仅原生 GPT | 服务器中心 | `server_codex_executor` | Server Center | Server 独立分区 |
| GPT + Gemini + WebGPT | 官方直连 | `browser_ai_bridge` | Hybrid 内部将 GPT 分流到 Direct | Bridge 分区 |
| GPT + Gemini + WebGPT | 浏览器完整 | `browser_ai_bridge` | Hybrid 内部将 GPT 分流到 Browser Full | Bridge 分区 |
| GPT + Gemini + WebGPT | 服务器中心 | `browser_ai_bridge` | Hybrid 内部将 GPT 分流到 Server Center | Bridge 分区 |

因此：

- “仅原生 GPT + 浏览器完整”和所有 Hybrid 组合都使用 `browser_ai_bridge`，属于同一个 Provider 分区；
- “仅原生 GPT + 官方直连”使用 `browser_ai_direct`，与 `browser_ai_bridge` 分开；
- “仅原生 GPT + 服务器中心”使用 `server_codex_executor`，也属于独立 Provider 分区；
- GPT-only 与 Hybrid 是否共享任务，不取决于模型目录里有几个模型，而取决于最终 `model_provider` 是否相同。

这里说的“共享”是 Codex 的 Provider 任务/历史分区：切到另一个 `model_provider` 时，不应假定能在当前
Provider 的任务视图里同时看到另一个 Provider 的任务。它不表示不同 Provider 之间会复制或同步会话。

## 为什么 Hybrid + Direct 仍是 Bridge Provider

Hybrid 的统一入口固定为：

```text
Codex
  -> browser_ai_bridge
  -> http://127.0.0.1:18888/hybrid/v1
  -> 根据当前模型和 hybrid-route.json 分流
       GPT          -> Direct / Browser Full / Server Center
       Gemini       -> Google 账号适配器
       chatgpt-web/* -> WebHarness
```

所以 `Direct` 在这里是 **GPT transport**，不是顶层 Provider。顶层 Provider 仍需要保留 Hybrid 的统一模型
识别和分流能力。

## 一个判断口诀

想判断“能看到哪些模型”，看 **Codex 模型模式**；想判断“任务属于哪个聊天分区”，看
**`model_provider`**；想判断“这一条 GPT 请求从哪里出去”，看 **原生 GPT 请求链路**。

更完整的操作说明见[客户端使用指南](USAGE.md)，底层数据流见[架构](ARCHITECTURE.md)。
