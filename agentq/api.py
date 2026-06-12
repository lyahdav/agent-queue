from __future__ import annotations

import html
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import ca_bundle_path, web_app_url
from .models import Project, Task


class QueueError(RuntimeError):
    pass


GET_RETRY_DELAYS = (1, 2, 4)
CLAIM_RETRY_DELAYS = (1, 2, 4)


class QueueClient:
    def __init__(self, url: str | None = None):
        self.url = url or web_app_url()
        self.ssl_context = _ssl_context()

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.url}?{query}"
        for attempt in range(len(GET_RETRY_DELAYS) + 1):
            try:
                with urllib.request.urlopen(url, timeout=60, context=self.ssl_context) as response:
                    return self._decode(response.read())
            except urllib.error.HTTPError as exc:
                if _is_retryable_http_error(exc) and attempt < len(GET_RETRY_DELAYS):
                    time.sleep(GET_RETRY_DELAYS[attempt])
                    continue
                raise QueueError(_url_error_message("GET", exc)) from exc
            except (urllib.error.URLError, OSError, TimeoutError, ssl.SSLError) as exc:
                if _is_retryable_transport_error(exc) and attempt < len(GET_RETRY_DELAYS):
                    time.sleep(GET_RETRY_DELAYS[attempt])
                    continue
                raise QueueError(_url_error_message("GET", exc)) from exc
        raise QueueError("queue GET failed")

    def _post(self, payload: dict[str, Any], *, retry_transient: bool = False) -> dict[str, Any]:
        operation = _payload_operation(payload)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        retry_delays = CLAIM_RETRY_DELAYS if retry_transient else ()
        for attempt in range(len(retry_delays) + 1):
            try:
                with urllib.request.urlopen(request, timeout=120, context=self.ssl_context) as response:
                    return self._decode(response.read())
            except urllib.error.HTTPError as exc:
                if _is_retryable_http_error(exc) and attempt < len(retry_delays):
                    time.sleep(retry_delays[attempt])
                    continue
                raise QueueError(_url_error_message("POST", exc, operation)) from exc
            except (urllib.error.URLError, OSError, TimeoutError, ssl.SSLError) as exc:
                if _is_retryable_transport_error(exc) and attempt < len(retry_delays):
                    time.sleep(retry_delays[attempt])
                    continue
                raise QueueError(_url_error_message("POST", exc, operation)) from exc
        raise QueueError(f"queue POST failed ({operation})")

    def _decode(self, body: bytes) -> dict[str, Any]:
        text = body.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            preview = text.replace("\n", " ")[:300]
            raise QueueError(f"queue returned non-JSON response: {preview}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise QueueError(str(data["error"]))
        if not isinstance(data, dict):
            raise QueueError("queue returned an unexpected JSON shape")
        return data

    def list_projects(self) -> list[Project]:
        data = self._get({"action": "projects"})
        return [Project.from_json(item) for item in data.get("projects", [])]

    def get_project(self, project_id: str) -> Project | None:
        for project in self.list_projects():
            if project.project_id == project_id:
                return project
        return None

    def claim(self, project_id: str, worker_id: str, resume_only: bool = False) -> Task | None:
        data = self._post(
            {
                "action": "claim",
                "projectId": project_id,
                "workerId": worker_id,
                "resumeOnly": resume_only,
            },
            retry_transient=True,
        )
        task = data.get("task")
        if not task:
            return None
        return Task.from_json(task)

    def update(
        self,
        project_id: str,
        task_id: str,
        status: str,
        sha: str = "",
        reason: str = "",
        last_error: str = "",
        runtime: str = "",
    ) -> None:
        payload = {
            "action": "update",
            "projectId": project_id,
            "id": task_id,
            "status": status,
            "sha": sha,
            "reason": reason,
            "lastError": last_error,
        }
        if runtime:
            payload["runtime"] = runtime
        data = self._post(payload)
        if data.get("success") is not True:
            raise QueueError(f"queue update failed for task {task_id}")

    def insert(self, project_id: str, tasks: list[dict[str, str]]) -> int:
        data = self._post({"action": "insert", "projectId": project_id, "tasks": tasks})
        if data.get("success") is not True:
            raise QueueError("queue insert failed")
        return int(data.get("inserted") or 0)


def _ssl_context() -> ssl.SSLContext:
    bundle = ca_bundle_path()
    if bundle:
        try:
            return ssl.create_default_context(cafile=bundle)
        except OSError as exc:
            raise RuntimeError(f"CA bundle path is not readable: {bundle}") from exc

    paths = ssl.get_default_verify_paths()
    if paths.cafile is None and paths.capath is None:
        certifi_bundle = _certifi_bundle()
        if certifi_bundle:
            return ssl.create_default_context(cafile=certifi_bundle)

    return ssl.create_default_context()


def _certifi_bundle() -> str | None:
    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        return None
    return certifi.where()


def _url_error_message(method: str, exc: BaseException, operation: str = "") -> str:
    if isinstance(exc, urllib.error.HTTPError):
        message = f"queue {method} failed: HTTP {exc.code} {exc.reason}"
        body = _http_error_body(exc)
        if body:
            message += f": {body}"
    else:
        message = f"queue {method} failed: {exc}"
    if operation:
        message += f" ({operation})"
    if _is_certificate_verify_error(exc):
        message += (
            ". TLS certificate verification failed. Run Python's "
            "Install Certificates.command on macOS, install certifi for this Python, "
            "or set AGENTQ_CA_BUNDLE in ~/.agent-queue/config.env to a PEM CA bundle."
        )
    return message


def _payload_operation(payload: dict[str, Any]) -> str:
    parts = []
    action = payload.get("action")
    if action:
        parts.append(str(action))
    project_id = payload.get("projectId")
    if project_id:
        parts.append(f"project={project_id}")
    task_id = payload.get("id")
    if task_id:
        parts.append(f"task={task_id}")
    status = payload.get("status")
    if status:
        parts.append(f"status={status}")
    if payload.get("resumeOnly"):
        parts.append("resumeOnly=true")
    return " ".join(parts)


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        text = exc.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return _clean_response_text(text)


def _clean_response_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if "<" not in stripped or ">" not in stripped:
        return " ".join(stripped.split())
    without_scripts = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", stripped)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    cleaned = html.unescape(without_tags)
    return " ".join(cleaned.split())


def _is_retryable_http_error(exc: urllib.error.HTTPError) -> bool:
    return exc.code in {500, 502, 503, 504}


def _is_retryable_transport_error(exc: BaseException) -> bool:
    if _is_certificate_verify_error(exc):
        return False
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, BaseException):
            return _is_retryable_transport_error(reason)
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionResetError,
            ConnectionAbortedError,
            ConnectionRefusedError,
            BrokenPipeError,
            socket.timeout,
        ),
    ):
        return True
    if isinstance(exc, ssl.SSLError):
        return _looks_like_transient_transport_error(str(exc))
    return _looks_like_transient_transport_error(str(exc))


def _looks_like_transient_transport_error(message: str) -> bool:
    text = message.lower()
    return any(
        phrase in text
        for phrase in (
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "connection refused",
            "broken pipe",
            "handshake operation timed out",
        )
    )


def _is_certificate_verify_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return (
        isinstance(exc, ssl.SSLCertVerificationError)
        or isinstance(reason, ssl.SSLCertVerificationError)
        or "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )
