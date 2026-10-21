"""Inert Muse workspace-skill probe; never executes commands or uses a network."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--challenge", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.challenge):
        parser.error("--challenge must be 32 lowercase hexadecimal characters")

    # Only this helper's own skill directory is writable. Each challenge is
    # single-use, allowing the caller to detect duplicate or invented runs.
    receipts = Path(__file__).resolve().parent.parent / "runs"
    receipts.mkdir(exist_ok=True)
    marker = "CODEX_MUSE_PROBE_" + secrets.token_hex(24)
    receipt = {
        "schema_version": 1,
        "challenge": args.challenge,
        "marker_sha256": hashlib.sha256(marker.encode("ascii")).hexdigest(),
    }
    try:
        with (receipts / (args.challenge + ".json")).open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle, separators=(",", ":"))
    except FileExistsError:
        parser.exit(2, "Challenge already used; receipt was not overwritten\n")
    print(json.dumps({"challenge": args.challenge, "marker": marker}, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
