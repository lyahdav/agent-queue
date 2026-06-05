import unittest

from agentq.api import QueueClient


class RecordingQueueClient(QueueClient):
    def __init__(self):
        self.payloads = []

    def _post(self, payload):
        self.payloads.append(payload)
        return {"success": True}


class QueueClientTests(unittest.TestCase):
    def test_update_sends_runtime(self):
        client = RecordingQueueClient()

        client.update("demo", "7", "VERIFY", sha="abc123", runtime="1m 5s")

        self.assertEqual(client.payloads[0]["runtime"], "1m 5s")
        self.assertEqual(client.payloads[0]["sha"], "abc123")

    def test_update_omits_empty_runtime(self):
        client = RecordingQueueClient()

        client.update("demo", "7", "VERIFY", sha="abc123")

        self.assertNotIn("runtime", client.payloads[0])


if __name__ == "__main__":
    unittest.main()
