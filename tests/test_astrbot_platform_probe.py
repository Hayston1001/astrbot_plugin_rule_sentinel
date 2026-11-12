import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


class AstrBotPlatformProbeTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_isolated_core_plugin_lifecycle_and_synthetic_message(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_instance_smoke.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Isolated instance smoke passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_actual_platform_queue_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_astrbot_platform.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("probe passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_passive_listener_sees_only_messages_that_passed_safety_stages(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_passive_listener.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Passive listener probe passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_synthetic_qq_adapter_snapshots_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_qq_snapshot.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("snapshots passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_actual_qq_callbacks_and_commit_tap_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_qq_commit_tap.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("commit tap probe passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_actual_waking_stage_with_unregistered_probe(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_waking_filter.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("comparison passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_actual_session_and_whitelist_stages_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_framework_session.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("session gate comparison passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_actual_scheduler_safety_stage_boundary_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_safety_scheduler.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scheduler behavior reproduced", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_synthetic_sdk_response_shapes_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_llm_response.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shape checks passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_provider_fallbacks_remain_an_explicit_risk(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_provider_retry.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fallbacks reproduced", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_request_guard_against_sdk_serialization(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_openai_guard.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guard passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_guarded_dry_run_isolated_integration(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_guarded_dry_run.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_plugin_web_request_objects_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_plugin_web.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("request probe passed", result.stdout)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not on PYTHONPATH")
    def test_real_dashboard_routes_and_auth_dependencies_offline(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_dashboard_auth.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("auth probe passed", result.stdout)
