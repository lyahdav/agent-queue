import unittest
from unittest.mock import patch

from agentq.logs import attach_command, format_log_timestamp, make_run_id


class LogFormattingTests(unittest.TestCase):
    def test_format_log_timestamp_includes_date_and_ampm_time(self):
        with patch("agentq.logs.time.strftime", return_value="2026-06-15 02:09:31 PM") as strftime:
            self.assertEqual(format_log_timestamp(), "2026-06-15 02:09:31 PM")

        strftime.assert_called_once_with("%Y-%m-%d %I:%M:%S %p")


class RunIdTests(unittest.TestCase):
    def test_make_run_id_names_the_agent(self):
        with patch("agentq.logs.time.strftime", return_value="20260625-181152"):
            run_id = make_run_id("GTasks", "138", "claude")

        self.assertEqual(run_id, "GTasks-138-claude-20260625-181152")

    def test_attach_command_built_from_run_id_makes_agent_clear(self):
        with patch("agentq.logs.time.strftime", return_value="20260625-181152"):
            run_id = make_run_id("GTasks", "138", "claude")

        command = attach_command(run_id, all_logs=True)

        self.assertEqual(
            command,
            "python3 -m agentq attach --run GTasks-138-claude-20260625-181152 --all",
        )
        self.assertIn("claude", command)


if __name__ == "__main__":
    unittest.main()
