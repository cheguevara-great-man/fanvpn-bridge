from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPAIR_SCRIPT = ROOT / "tools" / "repair_codex_project_mapping.mjs"


class CodexMappingRepairIntegrationTests(unittest.TestCase):
    def test_all_projects_restores_only_active_existing_vscode_threads(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temp = Path(temporary_directory)
            codex_home = temp / ".codex"
            codex_home.mkdir()
            project_a = temp / "project-a"
            project_b = temp / "project-b"
            missing_project = temp / "missing-project"
            project_a.mkdir()
            project_b.mkdir()

            state = {
                "thread-workspace-root-hints": {"active-a": str(temp)},
                "thread-writable-roots": {},
                "projectless-thread-ids": ["active-a", "active-b", "archived", "missing"],
                "electron-saved-workspace-roots": [],
                "project-order": [],
                "active-workspace-roots": [],
                "electron-persisted-atom-state": {},
            }
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )

            database = sqlite3.connect(codex_home / "state_5.sqlite")
            database.execute(
                """CREATE TABLE threads (
                    id TEXT, title TEXT, cwd TEXT, rollout_path TEXT,
                    source TEXT, archived INTEGER, recency_at INTEGER
                )"""
            )
            database.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("active-a", "A", str(project_a), "a.jsonl", "vscode", 0, 4),
                    ("active-b", "B", str(project_b), "b.jsonl", "vscode", 0, 3),
                    ("archived", "Archived", str(project_a), "c.jsonl", "vscode", 1, 2),
                    ("missing", "Missing", str(missing_project), "d.jsonl", "vscode", 0, 1),
                ],
            )
            database.commit()
            database.close()

            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(codex_home)
            completed = subprocess.run(
                [node, str(REPAIR_SCRIPT), "--all-projects", "--apply"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            result = json.loads(completed.stdout)
            repaired = json.loads(
                (codex_home / ".codex-global-state.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["scope"], "all-existing-projects")
            self.assertEqual(set(result["changedThreadIds"]), {"active-a", "active-b"})
            self.assertEqual(repaired["thread-workspace-root-hints"]["active-a"], str(project_a))
            self.assertEqual(repaired["thread-workspace-root-hints"]["active-b"], str(project_b))
            self.assertNotIn("active-a", repaired["projectless-thread-ids"])
            self.assertNotIn("active-b", repaired["projectless-thread-ids"])
            self.assertIn("archived", repaired["projectless-thread-ids"])
            self.assertIn("missing", repaired["projectless-thread-ids"])
            self.assertEqual(
                {Path(path) for path in repaired["electron-saved-workspace-roots"]},
                {project_a, project_b},
            )
            self.assertTrue(Path(result["backupPath"]).is_file())


if __name__ == "__main__":
    unittest.main()
