import io
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import package_plugin


class PackagingTests(unittest.TestCase):
    def test_default_output_uses_packaged_version(self):
        def archive_with_metadata(metadata):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("metadata.yaml", metadata)
            return buffer.getvalue()

        root = Path("example")
        for version in ("0.0.1", "'1.2.3'", '"2.0.0-rc.1"'):
            with self.subTest(version=version):
                data = archive_with_metadata(f"name: example\nversion: {version}\n")
                expected = version.strip("\"'")
                self.assertEqual(package_plugin.default_output(data, root),
                                 root / "dist" / f"astrbot_plugin_rule_sentinel-{expected}.zip")
        for metadata in ("name: example\n", "version: ../escape\n", "version: 1\nversion: 2\n"):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                package_plugin.default_output(archive_with_metadata(metadata), root)

    def test_cli_default_output_uses_metadata_version(self):
        with tempfile.TemporaryDirectory() as temp:
            expected = package_plugin.default_output(package_plugin.build_archive(), Path(temp))
            with patch.object(package_plugin, "default_output", return_value=expected):
                self.assertEqual(package_plugin.main([]), 0)
            self.assertTrue(expected.is_file())

    def test_archive_contains_runtime_files_without_local_data(self):
        data = package_plugin.build_archive()
        self.assertEqual(data, package_plugin.build_archive())
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            self.assertIsNone(archive.testzip())
            self.assertTrue({"main.py", "metadata.yaml", "_conf_schema.json", "README.md", "LICENSE", "sentinel/__init__.py",
                             "views/management/index.html", "views/management/app.js", "views/management/style.css",
                             "views/management/motion.js",
                             "scripts/evaluate_dry_run.py", "scripts/compare_dry_run.py"} <= names)
            self.assertFalse(any(name.startswith(("tests/", "local-docs/", ".git/", "dist/")) for name in names))
            self.assertEqual(archive.read("README.md"), (package_plugin.ROOT / "README.md").read_bytes())
            metadata = archive.read("metadata.yaml").decode("utf-8")
            description = next(line for line in metadata.splitlines() if line.startswith("desc: "))
            self.assertIn("群管理", description)
            self.assertIn("模型判定", description)
            self.assertFalse(any("__pycache__" in name or name.endswith((".pyc", ".sqlite", ".sqlite3")) for name in names))
            self.assertTrue(all(not name.startswith("/") and ".." not in Path(name).parts for name in names))

    def test_existing_output_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "plugin.zip"
            output.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                package_plugin.write_archive(output, b"new")
            self.assertEqual(output.read_bytes(), b"original")
            package_plugin.write_archive(output, b"new", force=True)
            self.assertEqual(output.read_bytes(), b"new")
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_archive_excludes_other_views(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in (*package_plugin.ROOT_FILES, *package_plugin.TOOL_FILES,
                             "sentinel/__init__.py", "views/management/index.html",
                             "views/management/app.js", "views/management/style.css",
                             "views/management/motion.js",
                             "views/management/nested/asset.js", "views/other/private.json"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic", encoding="utf-8")
            with zipfile.ZipFile(io.BytesIO(package_plugin.build_archive(root))) as archive:
                self.assertIn("views/management/nested/asset.js", archive.namelist())
                self.assertNotIn("views/other/private.json", archive.namelist())

    def test_missing_file_and_archive_size_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                package_plugin.build_archive(Path(temp))
        with patch.object(package_plugin, "MAX_ARCHIVE_BYTES", 1):
            with self.assertRaises(ValueError):
                package_plugin.build_archive()

    def test_cli_creates_archive_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "nested" / "plugin.zip"
            command = [sys.executable, "-B", str(package_plugin.ROOT / "scripts" / "package_plugin.py"),
                       "--output", str(output)]
            first = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(len(first.stdout.splitlines()), 1)
            original = output.read_bytes()
            second = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(output.read_bytes(), original)
            third = subprocess.run([*command, "--force"], capture_output=True, text=True, timeout=30)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(output.read_bytes(), original)

    def test_symlinked_runtime_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in (*package_plugin.ROOT_FILES, *package_plugin.TOOL_FILES,
                             "sentinel/__init__.py", "views/management/index.html",
                             "views/management/app.js", "views/management/style.css",
                             "views/management/motion.js"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic", encoding="utf-8")
            target = root / "sentinel" / "linked.py"
            try:
                os.symlink(root / "main.py", target)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links unavailable")
            with self.assertRaises(ValueError):
                package_plugin.build_archive(root)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_target_astrbot_accepts_and_extracts_archive_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "plugin.zip"
            archive.write_bytes(package_plugin.build_archive())
            code = (
                "from pathlib import Path; import sys; "
                "from astrbot.core.star.updater import _PluginUpdater; "
                "p=Path(sys.argv[1]); target=Path(sys.argv[2]); "
                "assert _PluginUpdater.validate_plugin_archive(str(p)) == 'metadata.yaml'; "
                "_PluginUpdater.__new__(_PluginUpdater)._extract_plugin_archive(str(p), str(target)); "
                "assert (target/'main.py').is_file(); "
                "assert (target/'sentinel'/'__init__.py').is_file(); "
                "assert (target/'views'/'management'/'index.html').is_file()"
            )
            env = {**os.environ, "ASTRBOT_ROOT": str(root / "astrbot")}
            result = subprocess.run([sys.executable, "-B", "-c", code, str(archive), str(root / "extracted")],
                                    capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
