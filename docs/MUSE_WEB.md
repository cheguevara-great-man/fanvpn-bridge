# Muse 网页模型接入（实验功能，默认关闭）

最新的显式可选路线见 [同轮 PC 工具接入](MUSE_PC_RPC.md)：已验证连续两次真实本机只读调用及原会话续聊；未自动部署。下文保留原 workspace 交接路线的说明和历史实验边界。

目标是让 `muse-web/default` 出现在 Codex 的 Hybrid 模型目录中，可作为主模型或具名子 Agent 模型。它不是额外的远程任务 MCP，也不替换现有 WebGPT、Gemini、Zen 或代理。模型名不承诺某个具体 Muse 版本、套餐权限或性能档位。

## 实现与限制

Codex → Bridge 的文本/工具适配 → 本机独立 Muse 服务 → Muse 网页账号的 VM。

- 私有协议使用 MIT 项目 [nikships/muse-cli](https://github.com/nikships/muse-cli)，固定提交 `ebcb6310432ad2d39927bcb59e686330b0538310`，来源见 `config/muse-web-source.json`。这是社区实现，不是 Meta 承诺稳定的公开网页 API。握手和会话接口来自该项目的实际协议实现，不是凭空猜测；仍需本账号实测。
- Native Host 仅使用标准库调用回环服务，不打包 Cookie 读取、Noise 或 curl_cffi。可选依赖安装在仓库 `dist-muse-web` 的隔离环境，不改日常 Python 环境。
- 服务只监听 `127.0.0.1:8008`，所有接口要求独立的至少 32 字符访问密钥；拒绝带 Origin 的浏览器请求，不提供 CORS。不使用 Cookie 充当本机 API key。
- 默认自动复用 Chrome 里的 Muse 登录：Muse 页面脚本调用同源的 `/api/session`、`/api/auth/check`、`/api/hatch/token`，经已有 Native Messaging 通道把短期连接票据交给本机服务。每条连接重新取票据，票据只在内存里传递，不导出 Cookie、不读取浏览器数据库、不增加 Cookie 权限，也不写入磁盘、日志或聊天。保留上游 TLS 校验，不修改浏览器安全策略，不绕过认证、设备校验或平台限制。
- 使用时须保留一个已登录的 `https://muse.ai/` 页面。升级扩展后，旧页面需要用户刷新或重新打开才能加载脚本。没有页面、登录过期、旧扩展不支持或 VM 不可用时明确报错；不自动打开网页、不切换账号、不尝试其他账号。多个候选页面优先当前活动页面，否则选择最近使用的非隐身 Muse 页面，不混用多个页面的票据。
- 每个新对话建立独立的 Muse 持久侧会话；续聊用完成的 response ID 找回会话。Codex 只重发完整历史、不带 `previous_response_id` 时，Bridge 也可凭同一 thread（优先）/`prompt_cache_key`，加上完整已提交历史前缀和新增输入，恢复当前 response ID，只向原 Muse 会话发送新增内容。不会仅凭问题相同或缓存键相同就拼接会话；编辑分支、控制指令/工具变化、无会话身份或 checkpoint 丢失时不猜测复用。该逻辑同时覆盖普通问答和 workspace 工具交接，压缩任务不覆盖普通对话的会话头。并行请求使用不同连接，不让多个读取器争抢 Noise 帧。最多并行 4 个请求，超出返回 429；同一会话并发请求明确拒绝。
- 当前通过 `chat.history` 查询侧会话回答；只接受明确的 `status=completed`，排除后台主动推送。等待期间发 SSE 心跳，完成后发送 Responses 正文和结束事件。**不是逐 token 实时流式**，也不凭“几秒没有变化”推断完成。
- Muse 工具请求使用独立的 workspace handoff，不再让模型在最终回答中伪造 XML。Muse 用现有 `muse.exec` 调用独立技能里的 JSON 辅助程序；服务读取已完成回合的请求，转换为 Codex 的 `function_call` / `custom_tool_call`。普通函数参数为 JSON 对象，freeform 保留原文；支持 namespace。工具目录先提供名称，完整定义按需读取，不把全部工具说明塞进每轮提示词。Gemini 的原有工具协议不变。
- 发送之后的网络异常不会自动重发；服务取消、超时或交付结果不明会使该会话快照失效，避免下一轮误接。重试前应在 Muse 网页确认任务状态。旧分支或重启丢失的快照只有在 Bridge 仍持有完整历史时才可重建新会话，不静默丢上下文。
- 只支持文本、自动工具选择和普通文本输出；不声明 OpenAI 内置搜索、图像能力或可调 reasoning 档位。目录的 32768 token 是适配层采用的保守上下文预算，不是已测得的 Muse 官方上限；本路线没有可验证的实际 token 用量上报。

**重要边界：Muse 网页是完整 Agent，不是纯推理 API。** 提示词要求它返回给 Codex 执行的工具调用，但这不是禁用 Muse 云端工具的强制机制；`capabilities={}` 也不能证明其全局插件、记忆和自动化已关闭。不同侧会话仍共享同一账号/VM 的权限与可能的长期记忆。首次验收仅进行无害问候、模拟工具返回，不给它文件修改、支付、删除或远程执行任务；本实现不修改 Muse 的 SOUL/MEMORY、现有 muse-bridge 插件或 VPS。

### 任务提示词边界

Muse 专用包装以最新用户请求及其输出格式为准；仅在请求需要时检查工作区、修改文件或执行命令。
用户明确要求不调用工具时直接回复，提供工具目录不代表必须使用工具。
保留客户端适用的任务指导、权限/审批规则、历史消息仅作上下文等约束，不要求模型切换身份。
此调整只影响 Muse，不改变 Gemini、WebGPT 等模型的提示词或工具协议。
包装由 Bridge/Native Host 组装；仅重启独立 Muse 服务不会更新正在运行的旧 Host，需要用户更新 Native Host 后生效。

### Workspace 工具交接（实验）

源码的正式工具路径与先前的随机探针分开：`tools/muse_tools/` 是技能模板、无执行能力的辅助程序和真实连接验收脚本。它不是 Codex 插件，也没有给 Muse 添加任意原生函数。

1. 每个生成回合新建随机 run 目录，只上传引用工具清单的小型 manifest。完整清单按规范化 JSON 的 SHA-256 保存于 `catalogs/<hash>.json`：内容不变时跨回合/新对话复用，定义变化后才上传新版本。服务重启后先回读校验已有文件；文件被删除则重新上传，不覆盖内容不符的文件。Muse 通过辅助程序读取目录/单项定义、提交至多一个请求，然后结束该回合；不让云端程序挂起等待用户审批。
2. 服务只在 Muse 明确完成后读取请求，检查 run、工具名称、类型和参数载体形状，不猜测或修复内容。Codex 接收正常工具调用，负责自己的参数/语法校验、审批及执行。辅助程序和服务均不执行该调用。
3. 下一次请求必须携带当前 response ID 和精确匹配的工具结果；若 Codex 重发完整历史，Bridge 在验证同一会话及已提交历史后可补全该 ID，独立服务仍严格核对 pending 调用。服务写入原 run 的结果文件，再在同一 Muse 会话开始下一回合，让辅助程序读取结果。工具清单变化、重复/错误/串会话的结果会拒绝；结果等待上限为 30 分钟。
4. 会话锁和完成快照限制并发。取消、超时、提交或结果写入不确定时不自动重发；丢失 pending 快照时不以新会话重演工具调用。取消是停止本地等待并使快照失效，**不是保证停止 Muse 所有云端工作**；但辅助程序本身没有后台等待任务。

`catalogs` 保留完整工具定义，run 目录保留清单引用、原始请求及工具输出（并非只有摘要）；它们会进入同一 Muse 账号的 VM 文件系统。随机编号和校验用于防串任务，不构成同一 VM 内的强安全隔离。当前不自动删除这些文件，以免误删审计材料；不应在首次试用中交付敏感任务。

清单、请求和结果的文件传输以最多 24 KiB 原始字节分块，避免 Base64/RPC 封装超过 Noise 单帧限制；写入临时文件，完整上传后才 rename 发布。总文件上限仍为 2 MiB，不截断、不修补工具内容；写入超时不自动重传。此分块只针对文件传输，不拆分用户问题、不增加模型回合，也不改变模型读取定义的 token 开销。

缓存清单的 manifest 使用版本 2，云端新版 helper 逐次核对清单内容哈希，并兼容旧版内联清单。旧 helper 不认识新引用格式，因此升级必须另建独立技能目录，**不要覆盖当前运行的技能**。服务在创建侧会话/发送消息前检查 helper 能力，不兼容则返回明确错误。先由用户安装新目录（名称须未使用），再自行停止旧 Muse 独立服务并选择新目录启动；不必为清单缓存单独重启 Bridge/代理：

```powershell
$env:PYTHONPATH = 'native-host'
.\dist-muse-web\Scripts\python.exe -m fanvpn_bridge.muse_tools_setup --skill-name codex_tools_cached_v2
# 安装成功后，用户自行停止旧 Muse 独立服务，再运行：
.\tools\start_muse_web.ps1 -ToolWorkspace workspace/custom-skills/codex_tools_cached_v2
```

安装及真实验收命令也支持 `--proxy`，选择规则与独立服务相同（参数优先，其次配置，默认 `auto`）。本地单元测试不连接账号；真实文件分块/模型往返仍应在用户明确部署新技能后运行下方验收脚本确认。

离线传输回归可用隔离环境里的固定版社区客户端运行；测试只在内存中完成双端 Noise 握手和文件系统模拟，不连接账号、不会消耗模型额度：

```powershell
$env:PYTHONPATH = 'native-host'
.\dist-muse-web\Scripts\python.exe -m unittest discover -s tests/unit -p 'test_muse_transport.py' -v
```

它会先复现旧版 70 KB 单次上传的 `NoiseInvalidMessage`，再校验新版大清单/长结果经过实际 protobuf、Noise 加密和解密后逐字一致、每帧低于限制，以及重复请求不再次上传清单。云端 `fs.write` 追加/`fs.read` 偏移的实际行为仍以真实连接验收为准。

2026-10-21 经用户授权的实际部署验收：独立安装 `workspace/custom-skills/codex_tools_cached_v2`，逐文件回读校验，保留旧技能。真实云端传输的 70,190 字节清单连续两轮只上传一次；240,090 字节的中文/换行/引号/路径结果分块写入和读回完全一致，这两项文件测试未调用模型。普通函数和 namespaced freeform 的真实模型往返均通过：精确输入、同一侧会话续聊、独立结果回读和技能源码未变化；分别约 92 秒和 98 秒，不代表速度保证。Muse 独立服务已选择新版目录，`service.json` 也已保存 `tool_workspace`；未重启 Bridge/代理、未更新 Native Host。此前的完整本地回归为 Muse 77 项、Gemini 49 项、HTTP gateway 43 项通过。

安装技能是独立、明确的云端操作：目标存在就拒绝覆盖，只写自己的 `SKILL.md`、`scripts/mailbox.py`、`skill.yaml` 并回读校验；不写现有插件、记忆、全局指令和本机配置。下面的安装命令仅供新的独立测试目录使用，已经安装的目录不要重复运行：

```powershell
$env:PYTHONPATH = 'native-host'
.\dist-muse-web\Scripts\python.exe -m fanvpn_bridge.muse_tools_setup --skill-name YOUR_UNIQUE_SNAKE_CASE_NAME
```

用户部署时，先自行停止旧 Muse 独立服务（不是 Bridge/代理），再显式指定安装输出的相对目录启动：

```powershell
.\tools\start_muse_web.ps1 -ToolWorkspace workspace/custom-skills/YOUR_UNIQUE_SNAKE_CASE_NAME
```

`-ToolWorkspace` 只传给本次启动的独立服务，不改本机配置；也可由用户在该服务的 JSON 配置里设置 `tool_workspace` 以便后续复用。不配置时工具请求会明确报错，不静默退回已验证失败的 XML 路径。Native Host 仍需由用户按下文命令更新，安装技能不会更新它。

无副作用真实验收（会建立独立 Muse 会话及 run 文件，但不执行请求的 shell/补丁）：

```powershell
.\dist-muse-web\Scripts\python.exe tools/muse_tools/verify.py --tool-workspace workspace/custom-skills/YOUR_UNIQUE_SNAKE_CASE_NAME
```

验收在随机临时回环端口启动测试服务，用普通函数、带 namespace 的 freeform 请求和调用后才生成的随机结果核对往返，并独立读取结果文件、确认技能源码未变。它不经过已安装的 Native Host，不证明完整代码任务或 Codex 实际审批界面已经验收。支持 `--only function` / `--only freeform` 单独定位；不自动重试失败的测试。

加 `--full-history` 可测试 Codex 重发完整历史、不带 `previous_response_id` 的工具结果续接路径。此处会话复用的修复位于 Native Host 中的 Muse provider；只重启独立 Muse 服务不会更新该逻辑，用户需自行更新 Native Host 后生效，不需要重装云端 helper 或刷新模型目录。

2026-10-21 的续聊修复验证：旧源码在本地模拟 Codex 完整历史请求时，连续三轮创建三个 Muse 会话；修复后同样请求只创建一个，后两轮不重复发送旧问题。Muse 84 项、Gemini 49 项、HTTP gateway 43 项及架构检查通过，覆盖普通/工具两条路径、流式/非流式、namespaced 工具结果、独立 Agent、编辑分支、控制变化、checkpoint 丢失和压缩任务。隔离临时服务上的真实两轮普通问答也确认云端 session ID 相同、第二轮仅发送新增输入；未更新或重启已安装的 Native Host、Muse 服务、Bridge 或代理。另一次 `--full-history` 真实工具测试首轮未产生预期工具请求，未达到续接阶段，不能据此宣称真实工具完整历史往返已经验收；该路径目前仅完成本地回归验证。测试新建的独立侧会话保留，未合并或删除用户已有会话。

## 准备（不部署、不启动服务）

```powershell
cd D:\software\AI_Coding\codex\deepseek-harness
.\tools\prepare_muse_web.ps1
.\tools\build_native_host.ps1 -SkipToolInstall -DistRoot .\dist-muse-bridge
```

准备脚本在目标已存在时拒绝覆盖。固定的是 Muse CLI 源码提交，其传递依赖仍按上游版本范围解析。编译仅输出独立 EXE，不更换当前运行的安装、不改注册表、计划任务、代理或模型目录。已有其他未提交源码修改也会包含在编译结果中。

## 用户确认后的手动部署顺序

**以下步骤会启动服务或更新安装，必须由用户决定并执行；不能把它们混进准备脚本。**

1. 在日常 Chrome 登录 Muse。在 `chrome://extensions` 重新加载本仓库的 `chrome-extension` 扩展，再刷新或重新打开 Muse 页面，保持登录。**不需要导出或填写 Cookie。**

2. 由用户运行启动脚本：

```powershell
.\tools\start_muse_web.ps1
```

首次运行自动生成随机本机 API key，并建立两份匹配的配置：

- `%LOCALAPPDATA%\FanVPNBridge\muse-web-auth\service.json`：独立服务配置。
- `%LOCALAPPDATA%\FanVPNBridge\muse-web.json`：Native Host 的 Muse 启用配置。

文件的 NTFS ACL 限制为当前 Windows 用户；本机 API key 不是 Muse 登录凭证。后续运行复用配置，已有配置冲突时拒绝覆盖。脚本不打印密钥、不重启 Bridge、不修改系统代理或开机启动，只在网络配置预检通过后启动独立服务并返回 PID；重复启动不会替换旧服务，端口被占用时新进程无法启动。使用现有自定义配置时可传 `-ConfigPath`，脚本不会为该配置自动生成 Native Host 设置。

自动生成的服务配置结构如下，示例不需要手动填写：

```json
{
  "api_key": "AUTOMATICALLY_GENERATED_LOCAL_SECRET",
  "port": 8008,
  "bridge_url": "http://127.0.0.1:18888",
  "proxy": "auto"
}
```

登录 HTTP 请求由 Chrome 的 Muse 页面发出；但后续 Noise/WebSocket 连接由独立服务发出，**不自动跟随 Chrome 插件所选节点**。服务启动时明确选择出口，不再依赖启动终端继承的代理环境：

- 默认 `auto`（旧配置缺少 `proxy` 字段时也适用）：检查已运行的 `127.0.0.1:18889` 是否返回本项目转发代理的健康标识，再使用它。只检查，不启动代理；检查失败则拒绝启动，不偷偷改为直连或其他代理。该检查不证明外网或 Muse 可达。
- `direct`：明确直连，清除服务进程继承的代理设置。仅适用于该电脑能直接访问 Muse 的网络。
- 代理 URL：支持有明确端口的 HTTP、HTTPS、SOCKS5、SOCKS5h 地址，例如 `http://127.0.0.1:7890`。`18888` 是 Bridge API，不能填成上网代理。

启动参数 `-Proxy` 优先于服务配置的 `proxy`，不会重写配置。选择只作用于 Muse 专用进程，不改变当前终端、系统代理、浏览器节点或已有 Gateway。回环接口始终绕过代理，自动/显式代理模式不继承可能让 Muse 绕过代理的 `NO_PROXY` 规则。网络和账号地区可用性仍须单独验证。

```powershell
# 默认检查并使用现有 18889；保留已经安装的独立工具技能
.\tools\start_muse_web.ps1 -ToolWorkspace workspace/custom-skills/codex_tools_handoff_6c0e71ab

# 也可显式指定其他现有代理，或选 -Proxy direct
.\tools\start_muse_web.ps1 -ToolWorkspace workspace/custom-skills/codex_tools_handoff_6c0e71ab -Proxy http://127.0.0.1:7890
```

代理凭证只能存于受保护的服务配置，不要放在命令行、聊天或日志中。切换出口需由用户停止旧 Muse 服务后重新启动；不需要为此重启 Bridge/代理。连接超时会报告 Muse 服务代理/VM 连接错误；流式请求使用终止性的 `response.failed`，不把失败伪装成成功或仅以 EOF 结束。Codex UI 的具体错误显示及是否重试仍需实际客户端验收。

3. 由用户更新 Native Host。自动登录需要新版扩展和新版 Host 两端，不能只更新独立服务。现有更新命令会停止旧 Bridge/代理，可能暂时影响正在使用的连接，选择安全时机执行：

```powershell
.\tools\update_native_host.ps1 -SkipToolInstall
```

更新脚本会自行编译；`dist-muse-bridge` 是提前验证用的独立产物，不是自动部署槽位。

Host 启动时读取步骤 2 生成的 `muse-web.json`，不需要手动编辑。也支持进程环境变量 `FANVPN_MUSE_WEB_URL` 和 `FANVPN_MUSE_WEB_API_KEY`；URL 环境变量优先。没有 LOCALAPPDATA 时使用 `~/.fanvpn-bridge/muse-web.json`。

4. 用本机密钥进行无害问候测试，**这一步才会实际访问 Muse 账号**：

```powershell
$museSettings = Get-Content "$env:LOCALAPPDATA\FanVPNBridge\muse-web.json" -Raw | ConvertFrom-Json
$museHeaders = @{ Authorization = "Bearer " + $museSettings.api_key }
Invoke-RestMethod 'http://127.0.0.1:8008/v1/responses' -Method Post -Headers $museHeaders `
    -ContentType 'application/json; charset=utf-8' -Body '{"model":"default","input":"Reply hello only. Do not use tools.","stream":false}' `
    -TimeoutSec 120
```

**模型目录只代表适配器入口存在，不证明登录、VM 或工具循环可用。** 问候成功后再做无害工具返回测试。登录过期时直接在 Muse 网页重新登录即可，不需要重新导出凭证或重建配置。

5. 刷新模型目录：

```powershell
Invoke-RestMethod 'http://127.0.0.1:18888/muse-web/v1/models?refresh=force' -Proxy $null
.\tools\refresh_model_catalog.ps1
```

重新打开 Codex 查看 `Muse Web (experimental)`。子 Agent 可选择 `muse-web/default`、reasoning `none`；主模型与子模型共用同一 provider，不额外启动浏览器。配置角色不会改变当前主模型。

通过 Browser Gateway 编辑默认子 Agent 的功能仍要求先处于已有的 Hybrid configured 模式；本接入不自动切换该模式，也不覆盖现有角色配置。

旧的自定义服务配置仍可显式指定 `cookies_file`，用于兼容已有的手工认证；默认流程不使用它，浏览器认证失败时也不会回退读取 Cookie。不要把 Cookie、连接票据或本机 API key 发进聊天。

## 关闭和验收

移走自己创建的 `muse-web.json`（保留备份）并取消相应环境变量，自行重启 Native Host、刷新目录；再仅停止启动脚本给出的 Muse PID。关闭 Muse 不需要切换网络模式，也不影响现有 WebGPT/Gemini。刷新失败时保留上次 Muse 目录，明确关闭时清除该模型入口。

自动测试使用模拟的 Muse 协议对象和临时回环端口，不访问真实账号。真实验收还应覆盖：账号登录与额度、VM 唤醒、无害工具结果续聊、长补丁、独立子 Agent、多任务串话、取消、限流、服务重启，以及网页升级后的状态字段变化。没有完成这些测试前，不应宣称已经支持真实 Muse 生产工作流。

### 本机实测记录（2026-10-20）

- 用户更新 Native Host 后，已刷新 Bridge 扩展和现有 Muse 页面。自动网页登录取票据成功，未导出 Cookie。
- 经 Bridge 的 `muse-web/default` 完成一次无害问候，返回 `MUSE_OK`，耗时约 7.1 秒。本地 Codex 目录已有 `Muse Web (experimental)`。
- 无害的 `muse_probe` 函数调用测试未通过。再次检查实际回复时，Muse 明确表示该函数不是其可用工具，且它使用自己的工具格式，不接受当前 Codex XML 工具协议。这是工具协议遵循的缺口，不是本次登录或网络连接失败；不能仅凭问候成功宣布完整 Agent 适配已验收。
- 未执行测试函数对应的本地动作，未修改 Muse 的全局指令、记忆、插件或账号设置。工具结果续聊及完整代码 Agent 流程仍待验证和解决。

### 原生客户端工具验证（2026-10-20）

已检查已登录网页实际加载的公开 JavaScript，不把登录页代码或社区客户端中的路由名称当作完整协议。以下资源可能随网页发布失效：

- `https://muse.ai/_next/static/chunks/05-ka1sp3k_nk.js`：`buildHatchClientCapabilities` 给出注册结构。SHA-256：`6b60756d452bf5a0332644be592146ff473139100b309fe684a3ccf5d803a4a3`。
- `https://muse.ai/_next/static/chunks/04jofo48yweaw.js`：按 `command_id` 分派 `client.invoke` 事件。
- `https://muse.ai/_next/static/chunks/0y-_qb3luk2ur.js`：通过 `client.invoke.result` 提交成功或失败，结果内容使用 `payload_json`。

观察到的注册请求包含 `client_id`、`platform`、`display_name`、`version` 和 `capabilities`。能力分为 `data_sources`、`device_commands`、`hatch_app_commands` 等；命令定义包含说明、必填/可选参数、权限、返回类型及超时。网页在连接代次变化时重新注册，但仅凭这一行为不能推断服务器的全部持久化和权限规则。

真实账号上的受限验证结果：

- 使用随机独立客户端标识，只注册无副作用的探针，不注册 shell、文件或云端操作；测试结束重新注册空能力，服务器均确认成功，再断开连接。没有创建或修改现有 Muse 插件、配对设备、全局指令或代理。
- 分别向 `device_commands` 和 `hatch_app_commands` 注册一个自定义命令，服务器对应的接受数均为 `1`，`rejected_ids=[]`。这只证明注册接口接受该定义，不证明模型能发现或调用它。
- `device_commands` 的独立侧会话测试返回 `UNAVAILABLE`，未收到探针的 `client.invoke`，未发送工具结果。
- `hatch_app_commands` 的独立侧会话测试在约 100 秒的观察窗口内没有得到最终回答，也未收到探针的 `client.invoke`；随后清空测试能力。不能把这次等待解释为工具调用成功，也不能仅凭超时断言该类别永远不支持自定义命令。
- 所有有效的侧会话结果检查都使用带 `session_id` 的历史查询，并排除提交前事件与后台推送。探针未执行任何实际本地动作；注册请求本身和创建测试侧会话属于本次有限的服务端状态变化，测试侧会话未删除。

**结论：原生工具调用及结果续聊仍未验收，不能用接口注册成功替代端到端验证。** 当前证据尚不能确定缺少的是会话工具暴露、客户端绑定还是其他服务端限制。下一步需要验证 Muse 侧正式支持的工具/插件入口；若涉及新增 VM 插件、设备配对或修改云端工具配置，应先单独征求用户同意。此次未把未经验证的原生调用路径加入运行中的 Bridge/独立服务，也未更新 Native Host。

### 独立 workspace 技能验证（2026-10-20）

用户同意尝试独立测试插件后，通过 Muse 自有工具读取当前环境的
`/opt/muse/skills/custom-connector-setup/SKILL.md` 及其技能模板，确认当前支持的是
workspace 技能与辅助程序；这些材料没有提供任意新增原生函数的安装流程。
旧的 `/opt/hatch/skills/skill-creator/SKILL.md` 已迁移，不能照旧快照的 frontmatter 格式安装。

本次仅在 Muse 云端新增测试目录：

```text
~/workspace/custom-skills/codex_tools_probe_7f2130bd/
  skill.yaml
  SKILL.md
  scripts/probe.py
  runs/<challenge>.json
```

- 安装前确认目标不存在；只上传三个模板/程序文件，逐文件回读校验。技能元数据在
  `skill.yaml`，`SKILL.md` 不带 frontmatter；设置 `include_in_prompt: false`，不将
  测试指令加入每轮提示词。
- 本地可复用源码在 `tools/muse_probe/`。辅助程序只接受严格校验的挑战值，生成随机
  字符串并在自身目录写入摘要收据，不调用网络、子进程或现有 muse-bridge。
- 两次独立的新 Muse 侧会话都被要求先用 `muse.skill_search` 查找该技能，再通过
  现有 `muse.exec` 运行辅助程序；不给模型读取程序源码或收据的任务，也不要求它
  输出伪造的 XML 工具调用。
- 两次均返回包含各自挑战值和不同随机字符串的 JSON。分别通过独立连接读取对应
  收据，验证挑战值、输出格式和 SHA-256 摘要，均一致。这里验证的是实际返回值与
  独立收据匹配，不是仅凭模型自称“执行成功”。
- 本地 Muse 相关 36 项测试通过，包括探针的随机结果、重复调用拒绝和路径输入校验。

**验收边界：这证明了 workspace 技能/辅助程序路线可用，不等于新增原生函数成功，
也不等于 Muse 已能调用 Codex 的 `exec_command`、`apply_patch`。** 正式接入仍需实现
调用队列、Codex 执行与审批、工具结果续接、超时/取消及会话隔离，再独立验收。
现有 Muse 文本/XML provider 未切换，Bridge、独立服务及 Native Host 未部署更新；
代理、现有 muse-bridge、账号权限和全局指令未修改。测试技能及两份收据保留在上述
独立目录，供后续复现；没有添加启动项或后台服务。

### Workspace 工具往返验收（2026-10-21）

本次新增的独立技能目录为 `workspace/custom-skills/codex_tools_handoff_6c0e71ab`；安装前检查不存在，安装后逐文件回读，未改旧探针、现有 muse-bridge、账号权限、记忆或全局指令。

真实验证发现，共享文本格式里的 `SYSTEM / DEVELOPER INSTRUCTIONS` 会被 Muse 视为伪装系统指令而拒绝。Muse 专用格式现改为如实说明来源的客户端任务/上下文，不冒充 Muse 系统角色、不改变其原生规则。Gemini 继续使用原有格式。

最终在同一次独立验收中通过：

- 普通函数：Muse 请求 `codex_probe`，参数中的随机 challenge 一致；测试端仅返回调用之后才生成的随机 marker；Muse 在同一侧会话准确读回。两个模型回合及校验共约 33 秒。
- namespaced freeform：Muse 请求 `functions.apply_patch`，中文、换行、反引号、引号、Windows 路径及末尾换行与指定输入逐字一致；测试端不应用补丁，只返回随机 marker；Muse 在同一侧会话准确读回。两个模型回合及校验共约 92 秒。
- 两项均另开认证连接，独立读取结果文件核对；技能的三个源文件测试后仍与本地渲染模板逐字一致。时间仅为这两次实验，不代表速度保证。
- Muse 54 项单元测试、Gemini 49 项回归测试、HTTP gateway 42 项集成测试以及架构检查通过。

源码已接入 workspace 路径，独立编译产物为 `dist-muse-handoff/browser-ai-bridge/`。验收完成时尚未部署或提交；云端仅保留自己的独立技能、测试侧会话和 run 文件。

这证明了真实 Muse → Codex Responses 工具请求 → 模拟工具结果 → 原 Muse 会话续接，不等于已经验证完整编码任务、Codex UI 审批、长期并发和网页升级兼容性。实际使用仍属实验；用户部署时，停止旧 Muse 独立服务后以本次目录启动：

```powershell
.\tools\start_muse_web.ps1 -ToolWorkspace workspace/custom-skills/codex_tools_handoff_6c0e71ab
```

之后再由用户选择安全时机执行 Native Host 更新命令。不要重复安装本次技能目录，也不要直接再次启动同端口服务而保留旧服务运行。

用户随后已手动完成部署。只读检查确认：注册和运行的 Native Host 均为新构建，Bridge ready，Muse 服务携带上述 workspace 参数；自动浏览器登录连接成功，云端三个技能文件仍与模板一致，Bridge 模型目录包含 `muse-web/default`。完整 Codex 编码任务仍需实际验收。
