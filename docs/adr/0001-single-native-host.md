# ADR-0001：使用单一 Native Host 进程

- 状态：Accepted
- 日期：2026-07-12

## 背景

v1 需要用户手工启动 HTTP SERVER；Chrome 又启动第二个 BRIDGE 进程，两者经 `127.0.0.1:18889` 通信。进程通过探测 `18888` 是否可连接来猜测角色。

## 决策

由 `runtime.connectNative()` 拉起的 Native Host 同时监听本地 HTTP 端口，并直接通过 stdin/stdout 与 Chrome 扩展通信。

## 理由

- 去掉一个进程、一个私有 TCP 协议跳点和角色猜测。
- Chrome Port 明确拥有 Host 生命周期。
- request id、取消、背压和断线处理只跨一个通道。
- 安装后用户不需要先启动常驻脚本。

## 后果

- Chrome 必须运行，本地 HTTP 端口才存在。
- 扩展 reload 时 Host 会重启，需要短暂 bind retry 和明确端口冲突错误。
- 发布版本应打包独立 EXE，避免 Chrome 启动批处理/Python 环境的不确定性。
