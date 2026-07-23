# Antigravity for VS Code 0.13.2 — Windows compatibility build

This directory contains the production `dist/extension.js` built from
[`lyadhgod/antigravity-vscode`](https://github.com/lyadhgod/antigravity-vscode)
commit `8380a5c7988c4bca6c215142993361acde0aeed4`.

The upstream extension is MIT-licensed; its license is included alongside the
bundle. The local compatibility change keeps the upstream behavior on Unix and
uses the ABI-compatible `node-pty` shipped by VS Code to create a Windows
ConPTY session. It also passes `vscode.env.appRoot` to the interactive-session
service so the bundled module can be located reliably.

This fixes the upstream Unix-only call to:

```text
script -q -e -c ... /dev/null
```

which otherwise makes every interactive Antigravity session exit before it is
ready on Windows.
