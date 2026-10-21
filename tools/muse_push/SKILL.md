---
name: __SKILL_NAME__
description: Request supplied PC tools over an authenticated connection and await the real client result.
---

# PC tool connection

Only use a connection/run explicitly supplied with the current task. The helper
requests tools from the user's client; it never executes the action in the cloud.
The client controls approval and actual execution. Respect all permission checks.

List tools: `python3 __SKILL_ROOT__/scripts/mailbox.py tools --run <run>`.
Read a definition with `--name <exact.name>`. Definitions are loaded only as needed.

Request a tool, with its JSON request on standard input:

```sh
python3 __SKILL_ROOT__/scripts/pc.py --run <run> <<'PC_REQUEST'
{"name":"exact.function.name","arguments":{"key":"value"}}
PC_REQUEST
```

For a custom/freeform tool use `{"name":"exact.name","input":"exact raw text"}`.
Preserve its input literally. Make calls sequentially, using the same run.
The helper shares a persistent HTTPS connection across sequential calls in this
run, using a private local IPC socket. No commands or results are exchanged via
files. Do not start/manage the connection owner yourself: pc.py does this.
The HTTPS call waits for the actual PC result. Continue from that result in the
same turn. If the cloud runner returns a process/session handle, use its normal
wait facility until the helper exits; a process handle is NOT the tool result.
Do not invent results or execute the PC action in the cloud.
No request/result files are used. Do not retry a failed or ambiguous call, switch
transports, or bypass a denied permission. Timeout does not mean the PC did not
execute: report the uncertainty and check with the user/client.
