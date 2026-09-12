# 文档导航

项目根目录的 [README](../README.md) 用于快速了解和安装。其余文档按读者任务组织：

## 使用者

1. [安装与升级](INSTALL_AND_UPDATE.md)：普通用户推荐入口；首次初始化后使用插件一键更新。
2. [Windows 完整安装参考](INSTALLATION.md)：手动构建、Native Host 注册、日志、回滚与卸载。
3. [客户端使用](USAGE.md)：配置 Codex、Claude Code、CC Switch 与 Gemini。
4. [Codex 路由与 Provider 速查](ROUTING.md)：快速判断 GPT-only、Hybrid、Direct、Browser Full 与聊天分区的关系。
5. [Antigravity CLI 浏览器链路](ANTIGRAVITY_CLI.md)：在 VS Code 终端中通过 Chrome 安装并运行官方 CLI。
6. [Codex + Gemini 账号](GEMINI_ACCOUNT.md)：保留 Codex Agent，用 Google 登录账号提供 Gemini 模型。
7. [Codex Hybrid](HYBRID_CODEX.md)：同一模型菜单使用 GPT/Gemini/WebGPT，并配置三种子 Agent 策略。
8. [ChatGPT 网页执行器](WEB_HARNESS.md)：安装和使用 WebHarness 网页模型链路。
9. [Codex 服务器中心 API](SERVER_CODEX_EXECUTOR.md)：部署 Server Lite、注册设备、切换链路与排障。
10. [Codex 用量上报](TOKEN_USAGE.md)：本机用量采集、额度同步和状态检查。
11. [故障排查](TROUBLESHOOTING.md)：Bridge、Chrome、FanVPN 或客户端异常时使用。

## 开发者

1. [架构](ARCHITECTURE.md)：系统边界、数据流、协议和安全模型。
2. [开发指南](DEVELOPMENT.md)：源码目录、测试、打包和改动原则。
3. [问题与解决记录](PROBLEM_SOLVING.md)：历史问题、根因、修复和可复用经验。
4. [服务器执行器总体方案](SERVER_CODEX_EXECUTOR_PLAN.md)：历史设计与阶段规划；当前行为以正式文档和代码为准。

## 文档维护规则

- `README.md` 只保留项目定位、最短安装路径和文档入口。
- `ARCHITECTURE.md`、`ROUTING.md`、`INSTALL_AND_UPDATE.md`、`INSTALLATION.md`、`USAGE.md`、
  `DEVELOPMENT.md`、`TROUBLESHOOTING.md`、`WEB_HARNESS.md` 只描述当前代码和当前操作。
- 开发阶段的失败方案、事故经过和版本演进只写入 `PROBLEM_SOLVING.md`。
- 带有 `*_PLAN.md` 的历史设计文档必须在顶部明确标注“历史设计”，避免被误认为当前实现规范。
- 命令或配置变更时，应在同一个提交中更新对应文档。
