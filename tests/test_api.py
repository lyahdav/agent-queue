import io
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


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def http_error(code, reason, body):
    return urllib.error.HTTPError(
        "https://example.test/queue",
        code,
        reason,
        {},
        io.BytesIO(body),
    )


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

    def test_http_errors_include_response_body(self):
        client = QueueClient("https://example.test/queue")
        error = http_error(500, "Internal Server Error", b"Apps Script lock timed out")

        with patch("agentq.api.urllib.request.urlopen", side_effect=error):
            with patch("agentq.api.time.sleep"):
                with self.assertRaises(QueueError) as raised:
                    client.list_projects()

        message = str(raised.exception)
        self.assertIn("HTTP 500 Internal Server Error", message)
        self.assertIn("Apps Script lock timed out", message)

    def test_get_retries_transient_http_errors(self):
        client = QueueClient("https://example.test/queue")

        with patch(
            "agentq.api.urllib.request.urlopen",
            side_effect=[
                http_error(500, "Internal Server Error", b"temporary"),
                Response(b'{"projects": []}'),
            ],
        ) as urlopen:
            with patch("agentq.api.time.sleep") as sleep:
                projects = client.list_projects()

        self.assertEqual(projects, [])
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_post_errors_do_not_retry_transient_http_errors(self):
        client = QueueClient("https://example.test/queue")

        with patch(
            "agentq.api.urllib.request.urlopen",
            side_effect=http_error(500, "Internal Server Error", b"temporary"),
        ) as urlopen:
            with patch("agentq.api.time.sleep") as sleep:
                with self.assertRaises(QueueError):
                    client.claim("demo", "worker-1")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_insert_does_not_retry_transient_http_errors(self):
        client = QueueClient("https://example.test/queue")

        with patch(
            "agentq.api.urllib.request.urlopen",
            side_effect=http_error(500, "Internal Server Error", b"temporary"),
        ) as urlopen:
            with patch("agentq.api.time.sleep") as sleep:
                with self.assertRaises(QueueError):
                    client.insert("demo", [{"task": "do it"}])

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_post_errors_include_payload_operation(self):
        client = QueueClient("https://example.test/queue")

        with patch(
            "agentq.api.urllib.request.urlopen",
            side_effect=http_error(500, "Internal Server Error", b"temporary"),
        ):
            with patch("agentq.api.time.sleep"):
                with self.assertRaises(QueueError) as raised:
                    client.update("demo", "7", "VERIFY", sha="abc123")

        message = str(raised.exception)
        self.assertIn("update project=demo task=7 status=VERIFY", message)

    def test_http_errors_extract_full_html_text(self):
        client = QueueClient("https://example.test/queue")
        body = b"""
            <!DOCTYPE html>
            <html>
              <head>
                <title>Error</title>
                <style>.errorMessage { font-weight: bold; }</style>
              </head>
              <body>
                <div><img alt="Google Apps Script"></div>
                <div class="errorMessage">
                  Exception: This is the important Apps Script failure,
                  including the later part that used to be cut off.
                </div>
              </body>
            </html>
        """

        with patch("agentq.api.urllib.request.urlopen", side_effect=http_error(500, "Internal Server Error", body)):
            with patch("agentq.api.time.sleep"):
                with self.assertRaises(QueueError) as raised:
                    client.list_projects()

        message = str(raised.exception)
        self.assertIn("Error Exception: This is the important Apps Script failure", message)
        self.assertIn("including the later part that used to be cut off", message)
        self.assertNotIn("font-weight", message)


if __name__ == "__main__":
    unittest.main()
