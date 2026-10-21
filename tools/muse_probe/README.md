# Muse workspace tool probe

This is a bounded experiment, not a new production transport or a Codex skill.
It uses Muse's existing `muse.exec` tool. It does not add a new native function
to Muse and does not execute any Codex tool.

The current Muse workspace-skill format was checked against the live runtime:

- `~/workspace/custom-skills/<name>/skill.yaml` holds metadata; `SKILL.md` has
  instructions only, with no frontmatter.
- The skill name and directory name must match and use lowercase snake case.
- `include_in_prompt: false` leaves discovery to `muse.skill_search` instead of
  adding instructions to every conversation.
- Auxiliary programs live under `scripts/` and use existing native tools.

`skill.yaml` and `SKILL.md` are templates. Before installation, replace
`__SKILL_NAME__` and `__SKILL_ROOT__` with a unique name and its own workspace
directory. Refuse an existing target. Install only these three files, verify
their bytes after upload, and do not change system skills, global instructions,
credentials, existing connectors, Native Host, proxies or permissions.

The helper accepts one 32-character lowercase hexadecimal challenge. It outputs
a random marker and records its SHA-256 digest in its own `runs/<challenge>.json`.
Receipt creation is exclusive: reusing a challenge fails without overwriting it.
An independent client reads the receipt and verifies the model's returned marker.
The helper does not accept commands, arbitrary paths, credentials or network URLs.

Offline tests:

```powershell
python -m unittest discover -s tests/unit -p test_muse_probe.py
```

The live test requires a signed-in Muse browser tab and the existing authenticated
Muse connection. No automatic installer or startup hook is supplied by this
experiment. The tested remote directory and results are recorded in
[`docs/MUSE_WEB.md`](../../docs/MUSE_WEB.md).
