# 开发指南

## 环境

- Python 3.12+：Native Host、测试和 PyInstaller 构建。
- Node.js 22+：Chrome 扩展测试与配置检查。
- Google Chrome 116+：真实 Native Messaging 和 FanVPN 端到端测试。

Native Host 运行时只使用 Python 标准库。PyInstaller 仅用于构建独立 EXE。

## 目录

```text
fanvpn-bridge/
├─ chrome-extension/       # MV3 扩展、Offscreen fetch 和 JS 测试
├─ native-host/            # loopback HTTP Gateway 与 Native Messaging Host
├─ config/                 # 路由和客户端配置示例
├─ contracts/              # 版本化 Native Messaging JSON Schema
├─ docs/                   # 面向使用者和开发者的文档
├─ tests/                  # Python unit / integration / e2e 说明
├─ tools/                  # 构建、安装辅助、诊断和验证脚本
├─ install.ps1
└─ uninstall.ps1
```

组件职责见[架构文档](ARCHITECTURE.md)。

## 测试

Python 核心、fake extension 和 HTTP Gateway：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'native-host')
python .\tools\run_v2_tests.py
```

Chrome 扩展：

```powershell
node .\tools\check_extension.mjs
node .\tools\check_extension_identity.mjs
node .\tools\check_offscreen.mjs
npm --prefix .\chrome-extension test
```

架构约束与打包产物：

```powershell
python .\tools\check_architecture.py
python .\tools\smoke_native_exe.py
```

真实 API 测试需要用户自己的 Chrome/FanVPN 和临时进程级凭据，不应作为无浏览器
CI 的成功证据。命令见[客户端使用](USAGE.md#路由验证)。

## 构建与安装开发版本

已安装环境使用 A/B 更新脚本，避免覆盖正在运行进程锁定的目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\update_native_host.ps1 `
  -Python "C:\path\to\python.exe"
```

脚本根据当前 Native Messaging 注册自动在 `dist-a` 和 `dist-b` 之间切换。刷新扩展、重开 Chrome，
再检查 `/ready`。首次安装仍按[安装文档](INSTALLATION.md)使用默认 `dist`。

## 改动原则

- Bridge 只做传输；AI wire API 转换留给 CC Switch 或其他适配器。
- 新上游必须加入静态 route 配置，不能接受客户端指定任意目标。
- 所有响应类型都使用同一字节流，不按 Content-Type 分叉缓存策略。
- 新协议 frame 必须先更新 JSON Schema 和双端校验，再更新测试。
- 任何 fallback 都必须证明仍经过 FanVPN；未经验证时保持失败关闭。
- 日志和测试输出不得包含 API Key、Authorization、Cookie 或完整请求正文。
- 不读取或修改 Codex/Claude 的任务数据库、侧栏状态和聊天历史。
- 改变用户操作、配置或架构时，同一提交必须更新对应文档。

## 发布前检查

1. 工作树只包含本次变更。
2. Python、Node 和架构检查全部通过。
3. 从干净输出目录构建 Native Host。
4. 使用真实 Chrome profile 验证扩展 ID、站点权限、`/ready` 和至少一个上游响应。
5. 检查日志和提交内容中没有密钥。
6. 更新 README 与 `docs/` 中受影响的当前行为。
7. 将开发过程中的新问题和根因补充到 `PROBLEM_SOLVING.md`。
