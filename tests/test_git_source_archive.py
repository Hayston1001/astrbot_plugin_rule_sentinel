import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import package_plugin


@unittest.skipUnless(shutil.which("git"), "Git is not available")
class GitSourceArchiveTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, check=True, timeout=30
        ).stdout

    def require_committed_checkout(self):
        root = package_plugin.ROOT
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        if result.returncode or Path(result.stdout.strip()).resolve() != root:
            self.skipTest("project is not a Git checkout")
        if subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root, capture_output=True, timeout=30,
        ).returncode:
            self.skipTest("Git checkout has no commits")
        return root

    def archive_files(self, root, *args):
        data = self.git(root, "archive", "--format=zip", *args)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIsNone(archive.testzip())
            return {name for name in archive.namelist() if not name.endswith("/")}

    def test_source_archive_matches_packager_for_committed_files(self):
        root = self.require_committed_checkout()
        tracked = {
            name.decode("utf-8")
            for name in self.git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0")
            if name
        }
        packaged = {path.as_posix() for path in package_plugin.archive_files(root)}
        source = self.archive_files(root, "--worktree-attributes", "HEAD")
        self.assertSetEqual(source, packaged & tracked)

    def test_committed_rules_exclude_known_development_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            included = {
                *package_plugin.ROOT_FILES,
                *package_plugin.TOOL_FILES,
                "sentinel/__init__.py",
                "sentinel/worker.py",
                "views/management/index.html",
                "views/other/index.html",
                "scripts/other.py",
                "notes.md",
                "new-directory/file.py",
            }
            excluded = {
                ".gitattributes",
                ".gitignore",
                ".github/workflows/tests.yml",
                "tests/test_example.py",
                "scripts/package_plugin.py",
                "scripts/probe_example.py",
                "scripts/probe_example.sh",
                "scripts/run_tests.py",
            }
            for name in included | excluded:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")
            (root / ".gitattributes").write_bytes(
                (package_plugin.ROOT / ".gitattributes").read_bytes()
            )
            self.git(root, "init", "--quiet")
            self.git(root, "add", "--all")
            tree = self.git(root, "write-tree").decode("ascii").strip()

            self.assertSetEqual(self.archive_files(root, tree), included)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_target_astrbot_accepts_source_archive_offline(self):
        root = self.require_committed_checkout()
        data = self.git(root, "archive", "--format=zip", "--worktree-attributes", "HEAD")
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            archive = temp_root / "plugin.zip"
            archive.write_bytes(data)
            code = (
                "from pathlib import Path; import sys; "
                "from astrbot.core.star.updater import _PluginUpdater; "
                "p=Path(sys.argv[1]); target=Path(sys.argv[2]); "
                "assert _PluginUpdater.validate_plugin_archive(str(p)) == 'metadata.yaml'; "
                "_PluginUpdater.__new__(_PluginUpdater)._extract_plugin_archive(str(p), str(target)); "
                "assert (target/'main.py').is_file(); "
                "assert (target/'logo.png').is_file(); "
                "assert (target/'sentinel'/'__init__.py').is_file(); "
                "assert (target/'views'/'management'/'index.html').is_file()"
            )
            env = {**os.environ, "ASTRBOT_ROOT": str(temp_root / "astrbot")}
            result = subprocess.run(
                [sys.executable, "-B", "-c", code, str(archive), str(temp_root / "extracted")],
                capture_output=True, text=True, timeout=90, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
