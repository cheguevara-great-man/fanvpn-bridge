# Codex Tools Probe

## Purpose
Verify that Muse can run an inert workspace helper and return its real result.
This is an integration experiment, not a shell, file or Codex executor.

## Tooling
Use the existing `muse.exec` tool to run exactly:

```text
python3 __SKILL_ROOT__/scripts/probe.py --challenge <challenge>
```

The caller provides the challenge: exactly 32 lowercase hexadecimal characters.
The helper generates a random marker, writes its SHA-256 receipt under this
skill's own `runs/` directory, and prints a JSON object with `challenge` and
`marker`. No network or subprocess is used by the helper.

## Auth
No credentials, account connections or permission changes are required.

## Operating Rules
1. Run the helper once for the caller's challenge; never invent its result.
2. Return the actual JSON object printed by the helper, without Markdown fences.
3. If execution fails, report the failure instead of simulating a success.
4. Do not read or edit receipts, helper source, other files, settings or memories.
5. Do not call existing muse-bridge tools or execute any other commands.
