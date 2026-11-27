import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

import cli_queue

THREAD = "00000000-0000-4000-8000-000000000001"
MESSAGE = "00000000-0000-4000-8000-000000000002"


class CliQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name).resolve()
        self.manifest = {"transport": "cli_queue", "thread_id": THREAD,
                         "codex_cli": sys.executable, "job_id": "job_fixture"}
        (self.run / "manifest.json").write_text(json.dumps(self.manifest))
        self.payload = {"delivery_id": "del_fixture", "event": {"job_id": "job_fixture", "type": "completed"},
                        "target": {"type": "local_command", "thread_id": THREAD, "callback_run_id": self.run.name}}
        self.execute = Mock(return_value=subprocess.CompletedProcess([], 0,
            f"Queued message {MESSAGE} for thread {THREAD}.\n", ""))

    def tearDown(self):
        self.tmp.cleanup()

    def test_admission_requests_resume_and_keeps_continuation_unconfirmed(self):
        receipt = cli_queue.deliver(self.run, self.payload, runner=self.execute)
        args = self.execute.call_args.args[0]
        self.assertEqual([sys.executable, "queue", "--thread", THREAD, "--message"], args[:5])
        self.assertIn("--resume", args)
        self.assertIn("del_fixture", args[5])
        self.assertIn("job_fixture", args[5])
        self.assertNotIn("shell", self.execute.call_args.kwargs)
        self.assertEqual("queued", receipt["state"])
        self.assertEqual(MESSAGE, receipt["queued_message_id"])
        self.assertTrue(receipt["resume_requested"])
        self.assertFalse(receipt["continuation_verified"])

    def test_resume_warning_does_not_lose_admission_or_requeue(self):
        self.execute.return_value.stderr = "Warning: could not resume thread\n"
        first = cli_queue.deliver(self.run, self.payload, runner=self.execute)
        second = cli_queue.deliver(self.run, self.payload, runner=self.execute)
        self.assertEqual("queued", first["state"])
        self.assertFalse(first["continuation_verified"])
        self.assertIn("could not resume", first["stderr"])
        self.assertEqual(first, second)
        self.assertEqual(1, self.execute.call_count)

    def test_resume_output_can_follow_one_exact_admission_line(self):
        self.execute.return_value.stdout += "Resume requested.\n"
        receipt = cli_queue.deliver(self.run, self.payload, runner=self.execute)
        self.assertEqual(MESSAGE, receipt["queued_message_id"])

    def test_multiple_admission_lines_are_uncertain_and_never_requeued(self):
        self.execute.return_value.stdout *= 2
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            cli_queue.deliver(self.run, self.payload, runner=self.execute)
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            cli_queue.deliver(self.run, self.payload, runner=self.execute)
        self.assertEqual(1, self.execute.call_count)

    def test_duplicate_delivery_does_not_enqueue_twice(self):
        first = cli_queue.deliver(self.run, self.payload, runner=self.execute)
        second = cli_queue.deliver(self.run, self.payload, runner=self.execute)
        self.assertEqual(first["queued_message_id"], second["queued_message_id"])
        self.assertEqual(1, self.execute.call_count)

    def test_uncertain_timeout_is_preserved_and_never_requeued(self):
        self.execute.side_effect = subprocess.TimeoutExpired("fixture", 20)
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            cli_queue.deliver(self.run, self.payload, runner=self.execute)
        self.execute.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "uncertain|in progress"):
            cli_queue.deliver(self.run, self.payload, runner=self.execute)
        self.assertEqual(1, self.execute.call_count)

    def test_zero_exit_without_matching_admission_is_not_delivered(self):
        self.execute.return_value.stdout = "No matching queued receipt"
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            cli_queue.deliver(self.run, self.payload, runner=self.execute)

    def test_wrong_job_thread_or_run_is_rejected_before_execution(self):
        for path, value in [("job", "job_other"), ("thread", MESSAGE), ("run", "other-run")]:
            with self.subTest(path=path):
                payload = json.loads(json.dumps(self.payload))
                if path == "job": payload["event"]["job_id"] = value
                elif path == "thread": payload["target"]["thread_id"] = value
                else: payload["target"]["callback_run_id"] = value
                with self.assertRaises(ValueError):
                    cli_queue.deliver(self.run, payload, runner=self.execute)
        self.execute.assert_not_called()

    def test_invalid_delivery_filename_is_rejected(self):
        self.payload["delivery_id"] = "../../outside"
        with self.assertRaises(ValueError):
            cli_queue.deliver(self.run, self.payload, runner=self.execute)
        self.execute.assert_not_called()

    def test_early_terminal_callback_can_precede_saved_submission_response(self):
        self.manifest.pop("job_id")
        (self.run / "manifest.json").write_text(json.dumps(self.manifest))
        receipt = cli_queue.deliver(self.run, self.payload, runner=self.execute)
        self.assertEqual("job_fixture", receipt["job_id"])
        self.assertNotIn("job_id", json.loads((self.run / "manifest.json").read_text()))

    def test_doctor_requires_an_existing_cli_and_queue_capability(self):
        execute = Mock(side_effect=[subprocess.CompletedProcess([],0,"codex-cli 0.166.0", ""),
                                    subprocess.CompletedProcess([],0,"queue --thread <id> --message <text> --resume", "")])
        result = cli_queue.doctor(sys.executable, THREAD, runner=execute)
        self.assertEqual("cli_queue", result["transport"])
        self.assertFalse(result["private_pipe_required"])
        self.assertTrue(result["resume_supported"])
        with self.assertRaises(ValueError):
            cli_queue.doctor("/missing-codex", THREAD, runner=execute)
        with self.assertRaises(ValueError):
            cli_queue.doctor(sys.executable, "invalid", runner=execute)

    def test_doctor_rejects_queue_without_resume_capability(self):
        execute = Mock(side_effect=[subprocess.CompletedProcess([], 0, "codex-cli fixture", ""),
                                   subprocess.CompletedProcess([], 0, "queue --thread <id> --message <text>", "")])
        with self.assertRaisesRegex(RuntimeError, "resume"):
            cli_queue.doctor(sys.executable, THREAD, runner=execute)


if __name__ == "__main__": unittest.main()
