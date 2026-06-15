import tempfile
import unittest
from pathlib import Path

from agentq.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_active_run_round_trip_and_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            store.update_run("demo", "run-1", status="AGENT", outputLog="/tmp/output.log")

            active = store.active_run_for_project("demo")
            self.assertIsNotNone(active)
            self.assertEqual(active["runId"], "run-1")
            self.assertEqual(active["status"], "AGENT")

            store.finish_run("demo", "run-1", status="VERIFY")
            self.assertIsNone(store.active_run_for_project("demo"))
            self.assertEqual(store.run("run-1")["status"], "VERIFY")

    def test_drain_flag_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")

            self.assertFalse(store.drain_requested())

            store.request_drain()
            self.assertTrue(store.drain_requested())

            store.clear_drain()
            self.assertFalse(store.drain_requested())


if __name__ == "__main__":
    unittest.main()
