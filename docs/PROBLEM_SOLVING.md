# 开发问题与解决记录

本文记录开发过程中遇到的问题、根因、最终解决方案和可复用经验。它不是安装
手册；当前操作请看[安装](INSTALLATION.md)、[使用](USAGE.md)和[排障](TROUBLESHOOTING.md)。

## 双进程原型生命周期复杂

**现象**：早期原型需要手工启动 HTTP Server，Chrome 再启动 Bridge，两个进程
通过第二个本地端口通信，并通过端口占用猜测角色。

**根因**：HTTP Gateway 与 Native Messaging Host 被错误拆成两个互相发现的进程。

**解决**：由 `runtime.connectNative()` 拉起的单一 Native Host 同时监听
`127.0.0.1:18888`，直接通过 stdin/stdout 与 Chrome 通信。

**经验**：让 Chrome Port 明确拥有 Host 生命周期，避免额外守护进程、端口和角色探测。

## Service Worker fetch 没有稳定经过 FanVPN

**现象**：普通 Chrome 页面能够访问目标网站，但扩展 Service Worker 的请求可能
绕过 FanVPN 或表现不同。

**解决**：所有真实出口统一放在 Offscreen Document 中；Service Worker 只管理
Native Port 和生命周期。Offscreen 不可用时直接失败，不回退系统直连。

**经验**：浏览器扩展的不同执行上下文不等同于普通标签页，网络路径必须逐一实测。

## Native Messaging 大消息限制

**现象**：包含大型工具定义、图片或长上下文的请求超过 Chrome 对 Host→扩展单条
消息约 1 MiB 的限制。

**解决**：head、body、end 分帧；body 使用 256 KiB chunk、序号和累计 ACK，每个
方向最多保留 4 个未确认 frame。

**经验**：Base64 和 JSON 也会放大数据，分片上限必须为封装留足空间。

## 流式响应直到上游关闭才出现

**现象**：VS Code 中请求已经到达上游，但界面长时间停在等待状态，直到完整响应结束才显示。

**根因**：Offscreen 的旧实现把读取到的 chunk 与结束标记耦合，破坏了首字节流式转发。

**解决**：`stream.js` 收到每个 chunk 后立即发送 `end=false`，读取完成后单独发送空的
`end=true` frame，并增加“上游尚未关闭时已收到首个 chunk”的回归测试。

**经验**：流式测试必须验证首个 chunk 的时间顺序，不能只验证最终拼接内容。

## 客户端断开后浏览器请求继续占用连接

**现象**：客户端重启后，新请求被多个最长 600 秒的旧请求拖住。

**根因**：本地 socket 断开没有及时传播到 Chrome 的 fetch。

**解决**：HTTP Gateway 轮询客户端断开，发送 `request.abort`；Offscreen 使用
`AbortController` 取消 fetch 并释放连接槽。

**经验**：超时不能替代取消，长流式请求必须把下游断开传播到最上游。

## Chrome 扣留扩展站点权限

**现象**：OpenAI/Gemini 的部分探测能返回 HTTP 响应，但 ChatGPT Codex route 在
Offscreen 中报 `Failed to fetch`。

**根因**：Chrome 在扩展重装或更新后将 Manifest Host Permissions 标记为 withheld。

**解决**：在扩展详情中将网站访问设为“在所有网站上”，并在弹窗和
`diagnose.ps1` 中显式报告授权状态。

**经验**：Manifest 声明权限不代表运行时已经授予，诊断必须展示实际 grant 状态。

## Gemini 请求触发浏览器 CORS 预检失败

**现象**：同一个 Gemini 请求从普通程序直连可用，经 Chrome Offscreen 时
`Failed to fetch`。

**根因**：Claude/SDK 添加的 `anthropic-*`、`x-app`、`x-stainless-*` 元数据 header
触发 Google 跨域预检，但 Gemini API 并不需要这些 header。

**解决**：Gemini route 使用请求头 allowlist，仅保留 `accept`、认证、内容类型等
必要 header。

**经验**：透明 body 传输不等于盲目转发所有跨协议元数据；浏览器 CORS 边界需要
按上游协议定义最小 header 集。

## Gemini 3 工具调用缺失 thoughtSignature

**现象**：简单文本可用，多轮工具调用返回 missing thought signature 400。

**根因**：Gemini 3 要求将模型返回的签名与对应 part 顺序在后续历史中原样回放。
这是有状态协议转换，不是网络转发问题。

**解决**：在 CC Switch 的 Gemini Native 适配器中保存和回放签名；Bridge 保持 JSON
body 字节透明。

**经验**：网络层与模型协议层必须分离，否则供应商协议升级会破坏稳定传输核心。

## Gemini API 拒绝当前节点地区

**现象**：Chrome 能打开 ChatGPT，但 Gemini 返回
`User location is not supported for the API use`。

**根因**：不同服务的地区策略不同，网页可达只证明网络工作，不证明节点满足 Gemini API 地区要求。

**解决**：切换 FanVPN 节点后重新验证 Gemini；不在 Bridge 或 CC Switch 中伪造地区信息。

**经验**：把“网络不可达”和“上游按地区拒绝”分开。收到真实 4xx 说明 Bridge 链路已通。

## CC Switch 全局接管影响了不相关 Claude 客户端

**现象**：为了让 VS Code Claude Code 使用 Gemini，CC Switch 启动时把
`~/.claude/settings.json` 改成 15721，导致全局 Claude CLI/客户端也被接管。

**解决**：`set_vscode_claude_mode.ps1` 只修改 VS Code 的
`claudeCode.environmentVariables`；启动 CC 代理后移除其 `PROXY_MANAGED` 全局接管值。

**经验**：协议转换代理应该按客户端显式选择，避免修改共享的全局配置。

## 系统代理截获 loopback 请求

**现象**：关闭或切换 Clash 后，VS Code 访问 `127.0.0.1:18888` 的表现变化。

**根因**：部分程序继承 `HTTP_PROXY/HTTPS_PROXY`，却没有正确排除 loopback。

**解决**：安装脚本将 `127.0.0.1,localhost` 合并进用户级 `NO_PROXY`，应用重启后生效。

**经验**：本地服务不能依赖代理软件恰好绕过 loopback；安装时应显式建立例外。

## Codex 侧栏任务缺失被误判为 Bridge 故障

**现象**：网络恢复后，Codex 某些任务仍只可搜索、无法在侧栏列出。

**结论**：Bridge 只承载 HTTP，不拥有 Codex 的任务索引和 UI 状态。尝试修复客户端
状态会扩大风险，并可能让网络问题与 UI 问题互相干扰。

**解决**：删除 Bridge 对 Codex 任务、项目映射、侧栏和本地数据库的恢复逻辑；
启动任务只启动 Chrome 并等待 Bridge ready。

**经验**：先用 `/ready` 和真实 API 请求证明网络层，再把客户端 UI 问题留在客户端边界内。

## 新电脑的 Codex OAuth Token Exchange 返回地区 403

**现象**：Chrome 中的 ChatGPT 登录页面成功，但回到 Codex 时显示 Token Exchange
被地区策略拒绝。

**根因**：浏览器授权页面使用 Chrome/FanVPN，而授权码换 Token 的 POST 请求由
本地 Codex 进程直接发送到 `auth.openai.com`；`chatgpt_base_url` 不控制 OAuth issuer。

**解决**：先新增固定的 `auth-openai` route，再实现一次性 Codex 登录助手。助手使用
Codex 官方 OAuth 参数生成 PKCE 和 `state`，让 Chrome 完成授权页面，并把授权码交换
请求通过 `auth-openai` 送出。成功后原子写入当前电脑自己的 `auth.json`，旧文件自动
备份，因此无需在电脑之间复制长期刷新凭据。后续刷新和注销继续使用相同 route。

**经验**：浏览器 OAuth 包含浏览器授权、localhost callback、本地 Token Exchange
和后续刷新四段网络路径；只验证登录网页不足以证明整个流程使用了同一出口。

## 构建成功但安装后仍运行旧版本

**现象**：`dist-next` 构建成功并包含新 route，但重新执行默认 `install.ps1` 后，`/routes` 仍没有新 route。

**根因**：构建输出目录与 Native Messaging 注册目录是两个独立选择。默认安装命令固定使用
`dist\browser-ai-bridge`，不会搜索或自动选择 `dist-next`。

**解决**：自定义构建目录时同步执行
`install.ps1 -BuildDirectory .\dist-next\browser-ai-bridge`，再刷新扩展并重开 Chrome。安装输出中的
`Native Host` 和 `Manifest` 路径是最终生效位置，应逐字核对。

**经验**：构建成功只说明产物已经生成，不代表 Chrome 已切换到该产物。部署验证必须同时检查安装路径、
`/ready` 和 `/routes`。

## 运行中的 Host 锁定 PyInstaller 输出目录

**现象**：重复构建时出现 `WinError 5`，常见被锁文件为 `_internal\libcrypto-3.dll`。

**根因**：Chrome 通过 Native Messaging 启动的 `browser-ai-bridge.exe` 正在从目标目录加载 DLL，
PyInstaller 无法先删除再重建该目录。

**解决**：关闭 Chrome 并等待 Host 退出后再构建；不希望中断当前链路时，使用新的 `-DistRoot`，构建成功后
再用匹配的 `-BuildDirectory` 原子切换注册路径。

**经验**：Windows 上不要原地覆盖正在运行的打包目录。保留“构建到新目录、验证、切换注册”的升级方式，
回滚也更简单。

**后续改进**：固定的 `dist-next` 在再次更新时也会成为正在使用的目录，因此最终采用 `dist-a` / `dist-b`
双槽轮换。`update_native_host.ps1` 读取当前 Native Messaging manifest，自动构建并注册非活动槽位；
每次切换后重开 Chrome，上一槽位就会被释放，供下次更新使用。

## 慢客户端阻塞所有并发请求

**现象**：一个本地客户端停止读取较大的流式响应后，其他 Codex 或 Claude 请求也无法继续收到响应。

**根因**：唯一的 Native Messaging reader 在线程内同步写入单个请求的有界队列；队列填满时 reader
被阻塞，无法再分发其他 request id 的 frame。

**解决**：reader 只做非阻塞分发，`flow.ack` 延迟到对应 HTTP 客户端实际消费 frame 后发送。浏览器的
协商窗口限制每个慢请求最多占用少量待消费 frame，同时 reader 可以继续处理其他请求。

**经验**：背压必须限定在单条流内；不能让一个消费者的背压占用全局复用通道的读取线程。

## 取消请求后 Offscreen 仍保留内存

**现象**：上传中断或响应在等待 ACK 时取消，请求已经从客户端消失，但 Offscreen 中仍可能保留正文 chunk、
fetch 和 Promise waiter。

**根因**：Host 的上传失败路径没有发送 `request.abort`；Offscreen 删除 Map 项时也没有唤醒等待流控 ACK
的 Promise。Native Port 断开同样没有批量取消在途请求。

**解决**：所有上传失败路径发送 best-effort abort；单请求取消、协议错误、重新握手和 Native Port 断开
都调用统一清理逻辑，终止 fetch、清空 chunk 并拒绝全部 waiter。

**经验**：从索引中删除对象不等于释放异步任务；必须显式终止持有该对象的等待链。

## 注册构建与运行进程发生版本错位

**现象**：仓库配置已经包含新 route，但 `/routes` 仍返回旧列表；旧诊断只显示各组件存在，没有报警。

**根因**：源码、Native Messaging 注册目录和 Chrome 已经拉起的进程属于三个不同状态，原诊断没有比较
它们。构建成功也没有证明新 EXE 能启动。

**解决**：诊断现在比较三组 route、注册与运行 EXE 路径及 route 文件哈希；A/B 更新在切换注册前执行
打包 EXE 冒烟测试，安装异常时恢复旧注册，并提供显式 `-Rollback`。

**经验**：部署健康不能只检查“文件存在”和“端口可达”，还必须证明源码、注册目标和实际进程一致。

## 为什么 VS Code 直连不能只设置一个全局代理

**问题**：希望 VS Code 可选地直连自有 HTTPS 代理，但不能影响 Windows 上其他软件；同时 VS Code
主程序和扩展并不完全使用同一套网络实现。

**解决**：新增只绑定 `127.0.0.1:18889` 的本地转发器。专用启动器同时传入 VS Code 官方
`--proxy-server` 参数和仅对子进程生效的标准代理环境变量，并维护 Codex provider；浏览器桥接仍为
默认模式。切换时要求先退出 VS Code，避免单实例进程复用旧环境。

**经验**：网络模式应当是显式、进程级且失败关闭的状态；不要用用户级 `HTTP_PROXY` 或系统代理
实现单个应用的选择功能。

## 浏览器链路中 Codex 第一条消息慢、后续消息快

**现象**：VS Code Codex 的模型对话已能通过 `127.0.0.1:18888/chatgpt-codex` 完成，但新开
VS Code 后第一条消息明显慢，后续消息恢复正常。Bridge 日志中的实际模型请求通常只有约一两秒，
不足以解释全部等待时间。

**根因**：自定义 `model_provider` 只改变模型请求和模型目录地址。Codex 启动时还会访问 Apps MCP、
插件目录和产品元数据等 ChatGPT 后端接口，这些请求由独立的 `chatgpt_base_url` 控制。旧配置没有修改该项，
因此在无 Clash 的电脑上仍由本地进程直连 `chatgpt.com`，直到超时后才继续；同一进程内初始化结果会
缓存或进入失败状态，所以后续消息较快。

**第一次判断（2.2.1 后，结论不完整）**：新增固定上游 `chatgpt-backend` route 后，日志显示多个
产品请求约 21 秒后同时失败，因而一度判断整条产品后端路线不可行。后来确认该次测试时浏览器代理
扩展没有开启；代理开启后，同一路由能够收到真实 200/404 响应，模型请求速度也恢复正常。因此“路由
必然造成重试风暴”不是可靠结论，真正未解决的是部分产品接口的语义兼容。

**稳定保底（Browser Lean）**：只保留已经验证稳定的模型数据通道，并临时设置
`apps=false`、`plugins=false`、`remote_plugin=false` 和 `analytics.enabled=false`。切换脚本保存每一项
原值，切回 Direct 时精确恢复，同时自动清理 2.2.1 遗留的 `chatgpt_base_url`。账号侧插件和 Apps
暂不可用，但核心对话、个人 Skills、本地脚本和手工配置的本地 MCP 保持可用。

**当前诊断（Browser Full）**：恢复固定的产品后端入口，但只在用户显式选择 Full 时写入
`chatgpt_base_url`。诊断开关把完整 URL、非敏感 header、状态和失败响应摘要用请求 ID 关联起来，
用于区分路径错误、缺失 header、账号权限、Cookie 依赖和真实上游功能差异。验证后的接口族再逐项
固化；Lean 始终作为不受实验影响的稳定保底。

**经验**：客户端的“模型 API 地址”和“产品后端地址”是两组配置。只看到模型流量成功，不能证明启动阶段
的所有网络请求都走了同一条链路；应同时检查 Codex 日志中的直连失败和 Bridge 各 route 的分段耗时。
