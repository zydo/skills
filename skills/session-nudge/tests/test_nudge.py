import io
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nudge


class FindTargetTests(unittest.TestCase):
    def test_api_worker_finds_the_unique_tui_in_the_project(self):
        shell = {"ppid": 1, "tty": None, "command": "bash -lc codex"}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(nudge.os.environ, {"CLAUDE_PID": ""}, clear=True))
            stack.enter_context(patch.object(nudge, "claude_sessions", return_value=[]))
            stack.enter_context(patch.object(nudge.os, "getppid", return_value=100))
            stack.enter_context(
                patch.object(nudge, "proc", side_effect=lambda pid: shell if pid == 100 else None)
            )
            stack.enter_context(
                patch.object(nudge, "agent_tuis_in_cwd", return_value=[(90636, "/dev/ttys018")])
            )
            stack.enter_context(patch.object(nudge.os, "getcwd", return_value="/workspace/app"))
            self.assertEqual(nudge.find_target(None), (90636, "/dev/ttys018"))

    def test_api_worker_refuses_to_guess_between_project_terminals(self):
        shell = {"ppid": 1, "tty": None, "command": "bash -lc codex"}
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(nudge.os.environ, {"CLAUDE_PID": ""}, clear=True))
            stack.enter_context(patch.object(nudge, "claude_sessions", return_value=[]))
            stack.enter_context(patch.object(nudge.os, "getppid", return_value=100))
            stack.enter_context(
                patch.object(nudge, "proc", side_effect=lambda pid: shell if pid == 100 else None)
            )
            stack.enter_context(
                patch.object(
                    nudge,
                    "agent_tuis_in_cwd",
                    return_value=[(10, "/dev/ttys010"), (20, "/dev/ttys020")],
                )
            )
            stack.enter_context(patch.object(nudge.os, "getcwd", return_value="/workspace/app"))
            stack.enter_context(redirect_stdout(output))
            self.assertIsNone(nudge.find_target(None))
        self.assertIn("--pid 10", output.getvalue())
        self.assertIn("--pid 20", output.getvalue())

    def test_macos_cwd_reader_parses_lsof_output(self):
        result = subprocess.CompletedProcess(
            ["lsof"], 0, "p90636\nf cwd\nn/workspace/app\n", ""
        )
        with patch.object(nudge, "SYSTEM", "Darwin"), patch.object(
            nudge, "run", return_value=result
        ):
            self.assertEqual(nudge.process_cwd(90636), "/workspace/app")


class OneTimeNudgeTests(unittest.TestCase):
    def test_local_time_rolls_to_tomorrow_when_it_has_passed(self):
        passed = time.mktime((2026, 10, 11, 2, 2, 0, -1, -1, -1))
        local = time.localtime(nudge.next_local_time("02:01", passed))
        self.assertEqual((local.tm_year, local.tm_mon, local.tm_mday), (2026, 10, 12))
        self.assertEqual((local.tm_hour, local.tm_min), (2, 1))

    def test_local_time_rejects_invalid_input(self):
        for value in ("25:00", "2:01", "02:60"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                nudge.next_local_time(value, 0)

    def test_scheduled_nudge_stays_queued_before_its_time(self):
        pid, tty = 90636, "/dev/ttys018"
        with tempfile.TemporaryDirectory(prefix="session-nudge-") as temp:
            state = Path(temp)
            include = state / "include"
            process = {"ppid": 1, "tty": tty, "command": "codex --yolo"}
            config = {"interval_minutes": 61, "message": "{now}: next {next}"}
            with ExitStack() as stack:
                stack.enter_context(patch.object(nudge, "STATE", state))
                stack.enter_context(patch.object(nudge, "INCLUDE", include))
                stack.enter_context(patch.object(nudge, "LOCK", state / "lock"))
                stack.enter_context(patch.object(nudge, "TEST_REQUEST", state / "test-request"))
                stack.enter_context(patch.object(nudge, "config", return_value=config))
                stack.enter_context(patch.object(nudge, "proc", return_value=process))
                stack.enter_context(patch.object(nudge, "is_agent_tui", return_value=process))
                stack.enter_context(patch.object(nudge, "log"))
                delivery = stack.enter_context(patch.object(nudge, "deliver_iterm2", return_value={tty}))
                stack.enter_context(patch.object(nudge, "deliver_tmux", return_value=set()))
                stack.enter_context(patch.object(nudge, "deliver_wezterm", return_value=set()))
                at = int(time.time()) + 120
                nudge.schedule_once(pid, tty, at)
                with patch.object(nudge.time, "time", return_value=at - 60):
                    nudge.fire_locked()
                self.assertIn("once_at=", include.read_text())
                delivery.assert_not_called()

    def test_one_time_delivery_is_removed_and_sent_only_once(self):
        pid, tty = 90636, "/dev/ttys018"
        with tempfile.TemporaryDirectory(prefix="session-nudge-") as temp:
            state = Path(temp)
            include = state / "include"
            config = {
                "interval_minutes": 61,
                "message": "[{now}] Automated nudge; next {next}",
            }
            process = {"ppid": 1, "tty": tty, "command": "codex --yolo"}
            sent = []
            with ExitStack() as stack:
                stack.enter_context(patch.object(nudge, "STATE", state))
                stack.enter_context(patch.object(nudge, "INCLUDE", include))
                stack.enter_context(patch.object(nudge, "LOCK", state / "lock"))
                stack.enter_context(patch.object(nudge, "TEST_REQUEST", state / "test-request"))
                stack.enter_context(patch.object(nudge, "config", return_value=config))
                stack.enter_context(patch.object(nudge, "proc", return_value=process))
                stack.enter_context(patch.object(nudge, "is_agent_tui", return_value=process))
                stack.enter_context(patch.object(nudge, "log"))
                stack.enter_context(patch.object(nudge, "deliver_tmux", return_value=set()))
                stack.enter_context(patch.object(nudge, "deliver_wezterm", return_value=set()))
                stack.enter_context(
                    patch.object(
                        nudge,
                        "deliver_iterm2",
                        side_effect=lambda targets, message: (sent.append(message) or {tty}),
                    )
                )
                nudge.schedule_once(pid, tty, int(time.time()) - 1)
                self.assertIn("once_at=", include.read_text())
                nudge.fire_locked()
                self.assertEqual(len(sent), 1)
                self.assertIn("none (one-time nudge)", sent[0])
                self.assertEqual(include.read_text(), "")
                nudge.fire_locked()
                self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
