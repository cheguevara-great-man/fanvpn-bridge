# Zen 免费模型接入

把 OpenCode Zen 上「不需要任何凭证即可调用」的模型，接进 Codex 的模型选择器。

## 定位

Codex 只说 Responses 协议（`wire_api = "responses"` 是唯一支持值），而 Zen 暴露的是 Chat Completions。所以这个 provider 干的唯一一件事是协议翻译。

和 DeepSeek Web、Gemini 账号两条链路不同，**Zen 不经过 Chrome**。上游是一个公开 HTTPS 端点，直接用 Python 客户端访问即可，因此不涉及 content script、PoW、非公开内部接口，也不依赖 FanVPN 出口。Chrome 不可用时 Zen 依然能工作。

```
Codex
  -> OpenAI Responses 协议
  -> 127.0.0.1:18888/zen/v1
  -> zen_provider.py 协议适配
  -> https://opencode.ai/zen/v1/chat/completions
```

Codex 依然是 Agent，工具循环、文件读写、终端、Skills 全在 Codex 侧。Zen 只提供模型推理。

## 模型发现与刷新

`cost: 0` **不等于**免鉴权可用。实测：models.dev 上标 0 成本的模型有 34 个，和 `opencode.ai/zen/v1/models` 实时列表求交集剩 11 个，逐个裸调后**只有 1 个真的返回 200**，其余是 400/403。所以不能只靠价格字段筛选。

`zen_models.py` 的刷新流程：

1. 拉 `https://models.dev/api.json`，取 `opencode` provider 下 `cost.input == 0 && cost.output == 0` 的条目
2. 与 `https://opencode.ai/zen/v1/models` 求交集（后者本身免鉴权）
3. 对交集里每个模型并发发一个最小请求（`max_tokens=8`，内容 `ping`），只保留 200 的
4. 结果写入 `%LOCALAPPDATA%\FanVPNBridge\zen-models.json`

探测用 6 个线程并发，11 个模型约十几秒，不会因为串行而卡住启动。

### 陈旧条目的处理

免费模型会下线。选择器里留着一条点进去才报错的僵尸条目比没有更糟，所以：

- 探测失败 → `consecutive_failures + 1`，条目**保留**
- 连续 3 次刷新都失败 → 从 catalog 移除
- 一次探测成功 → 计数清零

这条阈值写进缓存文件的 `failure_retirement_threshold`，改逻辑不需要动已有缓存。

### 何时刷新

- 冷启动且无缓存时，第一次 `GET /zen/v1/models` 触发
- 缓存文件超过 24 小时，下次读取时重新探测
- `GET /zen/v1/models?refresh=force` 强制重扫

## 手动刷新

```powershell
# 查看当前验证通过的免费模型，不改 Codex 配置
.\tools\refresh_zen_catalog.ps1 -ProbeOnly

# 强制重新探测 + 重写 Codex 模型目录
.\tools\refresh_zen_catalog.ps1 -Force
```

`refresh_model_catalog.ps1`（`start_vscode_network_mode.ps1` 会调）也会拉一次 Zen 模型列表，失败时保留上一次验证过的结果而不是清空。

## 命名

slug 全部带 `zen/` 前缀，display name 带 `Zen › ` 前缀。和其他 provider 完全不冲突：

| provider | slug | 显示名 |
|---|---|---|
| Zen | `zen/space-bunny-free` | `Zen › Space Bunny Free` |
| DeepSeek Web | `deepseek-web/reasoner` | `DeepSeek Web Reasoner` |
| ChatGPT Web | `chatgpt-web/high` | `ChatGPT Web — High` |
| Gemini | `gemini-3.7-flash-tiered` | `Gemini 3.7 Flash` |

### 关于二级菜单

**Codex 的模型选择器不支持分组。** 模型目录是扁平数组，条目之间没有任何关联字段（没有 `group` / `parent` / `family`）。picker 按 `priority` 升序排，同 priority 按 slug 字母序，然后平铺。GPT-6-Astra、GPT-5.6-Terra 这些官方模型也是同级平铺的。

替代手段是 `priority` 分段 + `display_name` 前缀。显示名的 `Zen › ` 前缀就是让这批条目在视觉上能一眼认出来的手段。

## 推理档位

从 models.dev 的 `reasoning_options` 读取，Codex 不认识的档位会被静默丢弃，所以公布完整 ladder 是安全的。目前验证通过的模型是 `low / medium / high / xhigh / max`。

Zen 认哪个参数名还没确认。`reasoning_effort`、`reasoning.effort`、`thinking.budget_tokens` 三种写法调用都成功，但用短回复测不出差异，需要一个真正需要推理的 prompt 再验证。`zen_provider.py` 目前统一发 `reasoning_effort`。

## 边界

- **免费额度随时可能变。** 上游改了策略，探测会失败，条目按上面的规则退役。这是预期行为，不是 bug。
- **不保证稳定。** 免费模型没有 SLA，可能随时下线或降级。建议用于非关键任务。
- **上游 403 会冒泡。** Cloudflare 会拒绝 `python-urllib` 默认 UA（error 1010），所以所有请求都带显式 `_USER_AGENT`。如果 UA 策略变化，症状是全部模型探测失败。
- **推理内容不转发。** 这批 catalog 条目标记 `supports_reasoning_summaries = false`，转发裸的 reasoning 事件会产生 Codex 无法挂到 output item 的孤儿事件。推理 token 仍计入 output usage。

## 相关文件

| 文件 | 作用 |
|---|---|
| `native-host/fanvpn_bridge/zen_models.py` | 发现、探测、缓存、退役 |
| `native-host/fanvpn_bridge/zen_provider.py` | Responses ↔ Chat Completions 翻译 |
| `native-host/fanvpn_bridge/http_server.py` | `/zen/v1/*` 路由与 hybrid 分发 |
| `tools/refresh_zen_catalog.ps1` | 手动刷新入口 |
| `tests/unit/test_zen_provider.py` | 27 个用例 |
