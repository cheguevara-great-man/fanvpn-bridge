# ADR-0002：Bridge 只做传输，不做 AI 协议转换

- 状态：Accepted
- 日期：2026-07-12

## 背景

Codex、Claude Code、OpenAI、Anthropic 和 Gemini 使用不同的 wire API。Gemini 3 还要求函数调用历史携带加密 thought signatures。若 Bridge 同时承担网络转发和 JSON 转换，任何模型协议升级都会威胁核心网络链路。

## 决策

Bridge 对 request/response body 保持字节透明，只做 route 选择、HTTP header 处理、流传输和错误分类。所有 AI 协议转换及会话状态由 CC Switch 或其他专用适配器负责。

## 后果

- Bridge 可同时承载 `/v1/responses`、`/v1/messages`、`/v1/chat/completions` 和原生 Gemini 请求。
- Gemini thought signature 修复必须在 CC Switch 仓库独立交付。
- 上游 4xx/5xx 必须原样回传，便于区分转换错误和网络错误。
