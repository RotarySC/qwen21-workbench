"""Exercise the Windows entry point in real linked worktrees, without a server."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import support


@unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows and Git required")
class WorktreeLauncher(support.LeakCheckedTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(dir=support.scratch_dir(), prefix="launcher-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.primary = self.root / "operational workspace" / "primary repo"
        self.primary.mkdir(parents=True)
        shutil.copyfile(Path(__file__).resolve().parents[1] / "run.bat", self.primary / "run.bat")
        package = self.primary / "workbench"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="ascii")
        (package / "server.py").write_text(
            "import json, os, sys\n"
            "print('LAUNCH_PROBE=' + json.dumps({\n"
            " 'source': os.path.dirname(os.path.dirname(os.path.abspath(__file__))),\n"
            " 'workspace': os.environ.get('QWEN21_WORKSPACE'),\n"
            " 'state': os.environ.get('QWEN21_STATE'),\n"
            " 'args': sys.argv[1:]}))\n", encoding="ascii")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Launcher Test", "-c", "user.email=test@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        self.linked = self.root / "linked worktree"
        self.git("worktree", "add", "--detach", "-q", str(self.linked))

    def git(self, *args):
        subprocess.run([shutil.which("git"), "-C", str(self.primary), *args],
                       check=True, capture_output=True, timeout=15)

    def launch(self, repo, **overrides):
        env = os.environ.copy()
        for key in ("QWEN21_WORKSPACE", "QWEN21_STATE", "QWEN21_PYTHON", "PYTHONPATH"):
            env.pop(key, None)
        env.update(QWEN21_PYTHON=sys.executable, **overrides)
        result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "run.bat", "8207"],
                                cwd=repo, env=env, input="\n", capture_output=True,
                                text=True, errors="replace", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [line.split("=", 1)[1] for line in result.stdout.splitlines()
                if line.startswith("LAUNCH_PROBE=")]
        self.assertEqual(len(rows), 1, result.stdout + result.stderr)
        return json.loads(rows[0])

    def test_linked_worktree_uses_its_own_code_and_primary_data(self):
        result = self.launch(self.linked)
        self.assertEqual(Path(result["source"]), self.linked)
        self.assertEqual(Path(result["workspace"]), self.primary.parent)
        self.assertEqual(Path(result["state"]), self.primary / "state")
        self.assertEqual(result["args"], ["--port", "8207"])

    def test_primary_checkout_keeps_existing_data_locations(self):
        result = self.launch(self.primary)
        self.assertEqual(Path(result["source"]), self.primary)
        self.assertEqual(Path(result["workspace"]), self.primary.parent)
        self.assertEqual(Path(result["state"]), self.primary / "state")

    def test_explicit_workspace_and_state_are_preserved(self):
        workspace = self.root / "custom workspace"
        state = self.root / "custom state"
        result = self.launch(self.linked, QWEN21_WORKSPACE=str(workspace), QWEN21_STATE=str(state))
        self.assertEqual(Path(result["source"]), self.linked)
        self.assertEqual(Path(result["workspace"]), workspace)
        self.assertEqual(Path(result["state"]), state)
