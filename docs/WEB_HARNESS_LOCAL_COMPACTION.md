# Codex 本地压缩兼容修复

## 2026-09-13 后续：频繁压缩与长篇收尾

11:05:41 的压缩后，Codex 本地有效上下文估计为 9725 token。
随后六份命令结果分别约为 10337、10138、14176、34、14036、3244 token，
合计 51965 token（项目的 o200k_base tokenizer），多份结果长达约四万字符。
11:07:13 请求用量达到 76108 token，再次触发压缩。这里没有证据表明摘要未清空旧历史
或 token 计算成倍累加；实际大范围代码和日志读取足以解释这次增长。

因此增加有界读取指引：探索命令默认请求约 2000 输出 token，限定文件和行范围；
巨大日志及响应状态文件只提取所需字段。摘要保留已确认事实、已检查范围和下一步，
要求后续轮避免重复整段调查。停止结果要求仅输出一句暂停确认，避免长篇任务答案。
这些是模型指令层的改进，并非硬截断工具结果，也不能保证模型绝不产生长回复；
上下文阈值和计数算法未修改。

2026-09-13，基于 `a4d053b` 修复。

## 直接证据

故障任务 `01a09453-4312-73c3-bb34-dd530814273e` 的本机
`WebHarness/responses-state.json` 中，响应 `resp_8f242d44b4014a2fa148a98806bb848f`
保存于 2026-09-12 23:10:51（北京时间）。其输入包含完整的
`You are performing a CONTEXT CHECKPOINT COMPACTION` 用户角色消息，输出却是
“查到了，而且现在原因范围已经非常小了……”的普通任务回答。
同一时间 Codex rollout 保存了 `compacted` 记录。压缩指令确实到达了执行器；
此前“没有压缩请求到达”的判断不成立。

Codex `rust-v0.153.4` 的 `codex-rs/core/src/compact.rs` 在本地压缩时通过普通
Responses 请求发送摘要提示词，并设置 `CodexResponsesRequestKind::Compaction`。
`responses_metadata.rs` 将其序列化到
`client_metadata["x-codex-turn-metadata"].request_kind = "compaction"`。
Codex 将返回的 assistant 文本作为摘要保存，不要求远程压缩的 `compaction` 输出项。

## 修改

WebHarness 原先仅识别 `/responses/compact` 和 `compaction_trigger`。
现在额外识别上述原生请求元数据，在计算执行标识和模型路由前进入已有压缩交接流程。
本地压缩仍返回普通 assistant 摘要文本；远程 v1/v2 保留原有输出协议。
成功的本地摘要也登记到后续任务恢复记录。

识别不依赖提示词内容、答案缓存或同一轮重复回答。用户普通消息和历史中的
`context_compaction` 标记不会触发新的压缩。

验证包括本地压缩的流式及非流式输出、压缩后继续任务、远程 v1/v2 回归，以及
已有交接流程拒绝将普通回答当作受控摘要的测试。真实历史用于确认根因，测试适配器
用于验证协议；这不等同于已经重跑用户整个长任务。

源码依据：
- https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/compact.rs
- https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/responses_metadata.rs
