# Windows 重启恢复

## 状态检查

```powershell
Invoke-RestMethod http://127.0.0.1:18888/health -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/ready -Proxy $null
Invoke-RestMethod http://127.0.0.1:18888/routes -Proxy $null
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\diagnose.ps1
```

`/health` 始终返回本地进程状态；`/ready` 只有在 Native Messaging 和浏览器执行器都可用时返回 200。响应包含 PID、运行模式、已加载路由、活动请求数和最近错误，不包含 Token、Cookie 或请求头值。

启动日志位于：

```text
%LOCALAPPDATA%\FanVPNBridge\startup.log
%LOCALAPPDATA%\FanVPNBridge\fanvpn-bridge.log
```

Native Host 日志按 2 MiB 轮转并保留三个历史文件；启动日志达到 1 MiB 时改名归档且不删除旧记录。两者只记录 PID、路由名、状态和错误码，不记录请求正文或请求头值。Codex 日志检查工具同样只输出错误元数据，不输出日志正文。

Codex 错误日志可以脱敏读取：

```powershell
node .\tools\inspect_codex_logs.mjs --errors
```

## 自动启动和恢复

`install.ps1` 注册当前用户任务计划 `FanVPN Bridge Bootstrap`。用户登录后，它会：

1. 在后台启动 Chrome；
2. 等待扩展拉起 Native Host；
3. 使用指数退避等待 `/ready`，最多三分钟；
4. 失败时由任务计划最多重试五次。

Chrome MV3 Service Worker 或 Native Messaging 断开后，扩展仍按 1～30 秒指数退避重连。任务计划使用 `IgnoreNew`，不会并行启动多个引导器；18888 仍由 Chrome 拉起的单一 Native Host 持有。

登录任务不能替用户授予 Chrome 站点权限。若扩展弹窗显示“ChatGPT 网站权限：被 Chrome 扣留”，请在 `chrome://extensions` 的 **FanVPN AI Bridge** 详情中选择“网站访问权限：在所有网站上”。这项权限属于 Bridge，而不是 FanVPN 节点开关。

手动运行一次引导器：

```powershell
Start-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap'
```

Bridge 的启动、安装和诊断脚本不会读取或修改 `.codex-global-state.json`、`state_5.sqlite`、任务归档状态、项目映射或侧栏偏好。Codex 客户端的任务显示问题应由 Codex 自身处理，不属于 Bridge 的恢复职责。

## 停止、重启和卸载

停止引导任务不会终止已经由 Chrome 启动的 Bridge：

```powershell
Stop-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap'
```

重新启动 Bridge 时，先关闭 Chrome，再结束路径位于本仓库构建目录的 `fanvpn-bridge.exe`，最后运行启动任务。Chrome 扩展会使用注册表当前指向的版本重新拉起它。

卸载 Native Messaging 注册和登录启动任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

这不会删除 `.codex`、会话、数据库、构建目录或启动日志。

## 回滚

1. 关闭 Codex 和 Chrome。
2. 运行 `uninstall.ps1`。
3. 导入备份的 `native-host-registration.reg`，恢复旧 Native Host 注册。
4. 重新打开 Chrome 和 Codex。

完整恢复备份目录由实施时记录；任何恢复操作都应在 Codex 进程退出后执行，避免客户端用内存状态覆盖文件。
