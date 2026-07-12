# 开发说明

## 当前阶段

本分支只建立 v2 的需求、架构、目录、模块边界和线协议。v1 原型继续保留，便于对照验证，但新功能不再继续堆叠到单文件 `bridge.py`。

## 开发原则

1. Bridge 是透明传输层，不做供应商 JSON 转换。
2. 任何网络 fallback 都必须先证明仍经过 FanVPN；默认失败关闭。
3. 不把 API Key 写进配置、日志、测试 fixture 或 Git。
4. Native Messaging 任一方向都必须分片，不能依赖当前请求“小于 1 MiB”。
5. 测试先使用 fake extension；真实 Chrome/FanVPN 测试单独标记，不能伪装成 CI 已通过。
6. 上游 HTTP 错误、Chrome 网络错误、桥接协议错误必须可区分。

## 计划中的 Python 包

```text
native-host/fanvpn_bridge/
├─ contracts.py       # 稳定接口与领域对象（已建立）
├─ errors.py          # 稳定错误码（已建立）
├─ config.py          # 配置加载、schema 校验、allowlist
├─ framing.py         # Chrome 4-byte native framing
├─ protocol.py        # v1 envelope 与分片/流控
├─ channel.py         # stdin/stdout 通道
├─ routing.py         # route -> upstream URL
├─ dispatcher.py      # request id 与在途请求状态机
├─ http_server.py     # loopback HTTP/1.1 gateway
├─ health.py          # 分层健康快照
└─ main.py            # 组合根和优雅退出
```

## 计划中的扩展模块

```text
chrome-extension/src/
├─ protocol.js        # 稳定契约与常量（已建立）
├─ native-port.js     # connectNative、握手、重连
├─ request-store.js   # 分片组装与 AbortController
├─ egress.js          # EgressExecutor 接口
├─ service-worker-egress.js
├─ offscreen-egress.js
├─ response-pump.js
└─ background.js      # 组合根
```

## 测试分层

- `tests/unit`：路由拼接、header 过滤、错误映射、状态机。
- `tests/contract`：schema、消息大小、seq、ack、版本协商。
- `tests/integration`：本地 HTTP Gateway 与 fake extension 的端到端流式测试。
- `tests/e2e`：需要用户 Chrome + FanVPN 的手工/受控测试。

首批必须覆盖：

- 大于 1 MiB 的请求体可分片通过；
- 100 MiB 响应不完整驻留内存；
- SSE chunk 边界任意拆分仍字节一致；
- 并发请求不会串流；
- 客户端取消能传播到 `AbortController`；
- 断开 Native Port 后在途请求得到确定错误；
- 绝对 URL、未知 route 和非 loopback bind 被拒绝；
- 日志不出现 Authorization 或 x-api-key 值。

## CC Switch 仓库边界

`cheguevara-great-man/cc-switch` 是独立交付物。当前 `fix/thought-signature` 分支包含 part-level signature 放置和 shadow state 相关代码；后续应在该仓库单独完成测试、CI 和可执行文件产物，不把补丁复制进 FanVPN Bridge。

## Git 工作流

- 架构阶段分支：`codex/architecture-foundation`
- 后续实现按可验证切片拆分分支/提交。
- 每个提交必须保持 schema 可解析、Python 可导入、测试可运行。
- 真实 API Key 与本机生成的 Native Host manifest 不提交。
