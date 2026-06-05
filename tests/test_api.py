import unittest
import urllib.error
from unittest.mock import patch

from agentq.api import QueueClient, QueueError


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

    def test_certificate_errors_include_tls_hint(self):
        client = QueueClient("https://example.test/queue")
        error = urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

        with patch("agentq.api.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(QueueError) as raised:
                client.list_projects()

        self.assertIn("TLS certificate verification failed", str(raised.exception))
        self.assertIn("AGENTQ_CA_BUNDLE", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
