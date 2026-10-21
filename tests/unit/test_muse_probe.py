from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[2] / "tools" / "muse_probe" / "scripts" / "probe.py"


class MuseProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.script = scripts / "probe.py"
        shutil.copyfile(SOURCE, self.script)

    def run_probe(self, challenge):
        return subprocess.run(
            [sys.executable, str(self.script), "--challenge", challenge],
            capture_output=True, text=True, timeout=10,
        )

    def test_real_random_marker_matches_independent_receipt(self):
        challenges = ["a" * 32, "b" * 32]
        markers = []
        for challenge in challenges:
            result = self.run_probe(challenge)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["challenge"], challenge)
            self.assertRegex(value["marker"], r"^CODEX_MUSE_PROBE_[0-9a-f]{48}$")
            receipt = json.loads((self.root / "runs" / (challenge + ".json")).read_text())
            self.assertEqual(receipt, {
                "schema_version": 1, "challenge": challenge,
                "marker_sha256": hashlib.sha256(value["marker"].encode("ascii")).hexdigest(),
            })
            self.assertNotIn(value["marker"], json.dumps(receipt))
            markers.append(value["marker"])
        self.assertNotEqual(*markers)

    def test_repeated_challenge_cannot_overwrite_receipt(self):
        challenge = "c" * 32
        self.assertEqual(self.run_probe(challenge).returncode, 0)
        path = self.root / "runs" / (challenge + ".json")
        before = path.read_bytes()
        result = self.run_probe(challenge)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("already used", result.stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_challenge_cannot_choose_file_paths(self):
        for challenge in ["../outside", "A" * 32, "a" * 31, "a" * 33, ""]:
            with self.subTest(challenge=challenge):
                result = self.run_probe(challenge)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertFalse((self.root / "runs").exists())


if __name__ == "__main__":
    unittest.main()
