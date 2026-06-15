import unittest
from unittest.mock import patch

from agentq.logs import format_log_timestamp


class LogFormattingTests(unittest.TestCase):
    def test_format_log_timestamp_includes_date_and_ampm_time(self):
        with patch("agentq.logs.time.strftime", return_value="2026-06-15 02:09:31 PM") as strftime:
            self.assertEqual(format_log_timestamp(), "2026-06-15 02:09:31 PM")

        strftime.assert_called_once_with("%Y-%m-%d %I:%M:%S %p")


if __name__ == "__main__":
    unittest.main()
