# 文档导航

项目根目录的 [README](../README.md) 用于快速了解和安装。其余文档按读者任务组织：

## 使用者

1. [Windows 安装](INSTALLATION.md)：构建、加载 Chrome 扩展、注册 Native Host。
2. [客户端使用](USAGE.md)：配置 Codex、Claude Code、CC Switch 与 Gemini。
3. [Antigravity CLI 浏览器链路](ANTIGRAVITY_CLI.md)：在 VS Code 终端中通过 Chrome 安装并运行官方 CLI。
4. [故障排查](TROUBLESHOOTING.md)：Bridge、Chrome、FanVPN 或客户端异常时使用。

## 开发者

1. [架构](ARCHITECTURE.md)：系统边界、数据流、协议和安全模型。
2. [开发指南](DEVELOPMENT.md)：源码目录、测试、打包和改动原则。
3. [问题与解决记录](PROBLEM_SOLVING.md)：历史问题、根因、修复和可复用经验。

## 文档维护规则

- `README.md` 只保留项目定位、最短安装路径和文档入口。
- `ARCHITECTURE.md`、`INSTALLATION.md`、`USAGE.md`、`DEVELOPMENT.md`、
  `TROUBLESHOOTING.md` 只描述当前代码和当前操作。
- 开发阶段的失败方案、事故经过和版本演进只写入 `PROBLEM_SOLVING.md`。
- 命令或配置变更时，应在同一个提交中更新对应文档。
