# 安装与升级

## 先说结论

- **首次安装**必须手动执行一次本机初始化，并在 Chrome 中对扩展目录执行一次“加载已解压的扩展程序”。这是 Chrome 的安全限制：一个已解压扩展不能自行给 Windows 安装 Native Host，也不能自行取得一个新目录的信任。
- **完成首次安装后**，打开 `FanVPN AI Bridge` 插件的“安装与升级”，点击相应按钮即可从 GitHub 下载、验证并升级 `FanVPN Bridge` 或 `Browser Gateway`。不再需要手动复制代码、构建 A/B 槽或运行 `install.ps1`。

更新包通过当前 Chrome 网络下载。Native Host 只接受固定的两个 GitHub 仓库、固定 `master` 分支的提交包；它会验证提交号、压缩包目录和所有解压路径，不接收 Cookie、账号密码或任意下载链接。

## 首次安装

先下载或克隆 `fanvpn-bridge` 源码。在该源码目录打开 **PowerShell**，运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\initialize_local_install.ps1
```

默认位置：

```text
%USERPROFILE%\Documents\fanvpn-bridge
%USERPROFILE%\Documents\browser-gateway
```

想使用自定义 Bridge 目录，在首次安装时指定：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\initialize_local_install.ps1 `
  -InstallRoot 'D:\AI\fanvpn-bridge'
```

脚本会构建 Native Host、注册 Chrome Native Messaging，并保留 A/B 两个运行槽，避免正在使用的 EXE 被覆盖。它需要已安装的 Python 3.12+；这是第一次构建所需的唯一开发依赖。

然后在 Chrome 打开 `chrome://extensions`：开启开发者模式 → **加载已解压的扩展程序** → 选择：

```text
%USERPROFILE%\Documents\fanvpn-bridge\chrome-extension
```

如果使用自定义目录，将上面的路径替换为你的自定义目录。Browser Gateway 同理，选择其 `extension` 子目录。

## 日常一键更新

1. 确保 Chrome、Browser Gateway（如使用）和 FanVPN AI Bridge 已连接。
2. 打开 **FanVPN AI Bridge** 弹窗。
3. 展开“安装与升级”。
4. 点击“更新 AI Bridge”或“更新 Browser Gateway”。

流程会：下载当前 GitHub `master` 提交的 ZIP → 校验 → 替换项目源码 → 对 AI Bridge 构建并注册未运行的 A/B 槽 → 自动重载已解压的 Chrome 扩展。

默认保留原安装目录。只有需要迁移目录时才填写目录输入框；也可以点击**选择文件夹**，由 Native Host 打开 Windows 系统文件夹选择窗口，选中的绝对路径会自动填入。迁移后的第一次，Chrome 必须由你在 `chrome://extensions` 中对新目录执行一次“加载已解压的扩展程序”；以后该新目录也支持一键更新。

## Browser Gateway 的边界

Browser Gateway 是纯 Chrome 扩展，不能直接写入 Windows 文件系统。因此它借用已安装的 AI Bridge Native Host 来完成下载与替换；两者安装在同一台电脑时，Gateway 弹窗和 Bridge 弹窗都可以触发 Gateway 更新。

若电脑尚未安装 AI Bridge，Gateway 的**首次安装**仍需手动“加载已解压的扩展程序”。这是 Chrome 的硬性安全模型，不是项目遗漏。若将来发布到 Chrome Web Store，Chrome 会在启动后和定期检查时自动安装扩展更新；目前已解压加载模式则由本项目的“下载 + `runtime.reload()`”完成即时更新。
