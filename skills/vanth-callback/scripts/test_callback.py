"""Local behavior checks; never contact Desktop or submit Vanth jobs."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("callback_under_test", Path(__file__).with_name("callback.py"))
callback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(callback)


class MacCallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vanth-test-")
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX session behavior")
    def test_spawn_detaches_without_windows_flags(self):
        script = self.root / "worker with ' quote.py"
        script.write_text("import os,json,sys;print(json.dumps({'pid':os.getpid(),'sid':os.getsid(0),'args':sys.argv[1:]}))")
        with patch.object(callback, "SCRIPT", script):
            child = callback.spawn(self.root, "probe")
        try:
            self.assertEqual(0, child.wait(timeout=5))
            data = json.loads((self.root / "probe.log").read_text())
            self.assertEqual(child.pid, data["sid"])
            self.assertNotEqual(os.getsid(0), data["sid"])
            self.assertEqual(["probe", "--run", str(self.root)], data["args"])
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

    @unittest.skipIf(os.name == "nt", "POSIX shell quoting")
    def test_command_line_preserves_argument_boundaries(self):
        script = self.root / "worker's script.py"
        script.write_text("import json,sys;print(json.dumps(sys.argv[1:]))")
        args = [sys.executable, str(script), "a b", "single'quote", "$HOME", "semi;colon"]
        line = callback.command_line(args)
        self.assertEqual(args, shlex.split(line))
        completed = subprocess.run(line, shell=True, capture_output=True, text=True, check=True)
        self.assertEqual(args[2:], json.loads(completed.stdout))

    @unittest.skipIf(os.name == "nt", "POSIX private directory mode")
    def test_run_is_private_before_first_state_is_written(self):
        run_root = self.root / "runs"
        task = self.root / "spec.json"
        task.write_text(json.dumps(dict(args=[sys.executable, "-c", "pass"], cwd=str(self.root), timeout_seconds=30, resume_prompt="Inspect only local test output")))
        previous = os.umask(0o022)
        try:
            with patch.object(callback, "ROOT", run_root), patch.object(callback, "doctor", return_value={}), patch.object(callback, "configure", side_effect=RuntimeError("stop before daemon")):
                with self.assertRaisesRegex(RuntimeError, "stop before daemon"):
                    callback.submit(task)
            run, = list(run_root.iterdir())
            self.assertEqual(0o700, run.stat().st_mode & 0o777)
            self.assertEqual(0o700, run_root.stat().st_mode & 0o777)
        finally:
            os.umask(previous)

    def test_resolve_run_rejects_other_task_roots(self):
        run_root = self.root / "runs"
        run_root.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "manifest.json").write_text("{}")
        with patch.object(callback, "ROOT", run_root), self.assertRaises(ValueError):
            callback.resolve_run(outside)

    def test_failed_doctor_never_creates_a_run_or_background_process(self):
        run_root = self.root / "blocked-runs"
        task = self.root / "spec.json"
        task.write_text(json.dumps(dict(args=[sys.executable, "-c", "pass"], cwd=str(self.root), timeout_seconds=30, resume_prompt="Inspect only local test output")))
        with patch.object(callback, "ROOT", run_root), patch.object(callback, "doctor", side_effect=RuntimeError("host unavailable")), patch.object(callback, "spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "host unavailable"):
                callback.submit(task)
            self.assertFalse(run_root.exists())
            spawn.assert_not_called()

    def test_cli_submission_uses_local_command_and_no_desktop_relay(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        run_root = self.root / "runs"
        task = self.root / "spec.json"
        task.write_text(json.dumps(dict(args=[sys.executable, "-c", "pass"], cwd=str(self.root), timeout_seconds=30, resume_prompt="Inspect only local test output")))
        client = Mock()
        client.post.return_value = {"job_id": "job_fixture", "status": "running"}
        preflight = {"transport": "cli_queue", "thread_id": "00000000-0000-4000-8000-000000000001", "codex_cli": sys.executable}
        with patch.object(callback, "ROOT", run_root), patch.object(callback, "doctor", return_value=preflight), patch.object(callback, "configure", side_effect=lambda run: (callback.read(run / "manifest.json"), client)), patch.object(callback, "ready", return_value=True), patch.object(callback, "spawn", return_value=SimpleNamespace(pid=1234)) as spawn, patch("vanth.setup.register_codex_desktop", side_effect=AssertionError("Must not register a private pipe")):
            result = callback.submit(task)
        run = Path(result["run"])
        request = callback.read(run / "request.json")
        self.assertEqual("local_command", request["wake_targets"][0]["type"])
        self.assertEqual(1, request["wake_targets"][0]["max_attempts"])
        self.assertIn("queue-notify", request["wake_targets"][0]["command"])
        self.assertEqual(["daemon"], [c.args[1] for c in spawn.call_args_list])

    def test_cli_cleanup_requires_completed_continuation_and_has_no_relay_wait(self):
        from unittest.mock import Mock
        manifest = {"transport": "cli_queue", "job_id": "job_fixture"}
        client = Mock()
        terminal = {"status": "completed", "deliveries": [{"status": "delivered", "delivery_id": "del_fixture"}]}
        with self.assertRaisesRegex(RuntimeError, "finish|Finish|continuation"):
            callback.cleanup_run(self.root, manifest, client)
        receipt = {"state": "completed", "job_id": "job_fixture", "delivery_id": "del_other"}
        (self.root / "receipt.json").write_text(json.dumps(receipt))
        with patch.object(callback, "snapshot", return_value=terminal), patch.object(callback, "ready", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                callback.cleanup_run(self.root, manifest, client)
            client.post.assert_not_called()
        receipt["delivery_id"] = "del_fixture"
        (self.root / "receipt.json").write_text(json.dumps(receipt))
        with patch.object(callback, "snapshot", return_value=terminal), patch.object(callback, "ready", side_effect=lambda c: not c.post.called):
            result = callback.cleanup_run(self.root, manifest, client)
        client.post.assert_called_once_with("/shutdown", {})
        self.assertTrue(result["daemon_stopped"])
        self.assertFalse((self.root / "relay-stop.flag").exists())


if __name__ == "__main__":
    unittest.main()
