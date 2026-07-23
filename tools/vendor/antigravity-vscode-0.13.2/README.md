# Antigravity for VS Code 0.13.2 — Windows compatibility build

This directory contains the production `dist/extension.js` built from
[`lyadhgod/antigravity-vscode`](https://github.com/lyadhgod/antigravity-vscode)
commit `8380a5c7988c4bca6c215142993361acde0aeed4`.

The upstream extension is MIT-licensed; its license is included alongside the
bundle. The local compatibility change keeps the upstream behavior on Unix and
uses the ABI-compatible `node-pty` shipped by VS Code to create a Windows
ConPTY session. It also passes `vscode.env.appRoot` to the interactive-session
service so the bundled module can be located reliably. On Windows it detects
the official `gemini:antigravity` Credential Manager entry instead of relying
on the legacy plaintext token-file probe; it checks only the target name and
never reads the credential secret. Visible lifecycle commands use an explicit
`powershell.exe -NoProfile` terminal and the PowerShell call operator, so a
quoted Windows executable path is executed instead of merely echoed. Google
sign-in opens in a separate PowerShell window on Windows; the extension polls
Credential Manager and refreshes its chat view automatically when sign-in
finishes. Interactive screen rendering uses an 80 ms fixed-frequency throttle
instead of the upstream trailing-edge debounce. This lets partial assistant
text reach the VS Code chat view throughout generation instead of commonly
appearing all at once when generation ends. The screen parser also understands
the Antigravity CLI 1.1.5 model picker, where the new Effort slider separates
the model rows from the keyboard footer; `/model` therefore surfaces clickable
model choices instead of leaving the hidden TUI waiting indefinitely. Local
images generated inside an open workspace are converted to restricted webview
resource URIs and rendered inline; the compatibility package therefore also
includes the corresponding `media/main.js` and `media/main.css`.

This fixes the upstream Unix-only call to:

```text
script -q -e -c ... /dev/null
```

which otherwise makes every interactive Antigravity session exit before it is
ready on Windows.
