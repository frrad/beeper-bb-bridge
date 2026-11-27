"""Exercise outage/recovery transitions and delivery failures without real mail."""

from __future__ import annotations

import io
import base64
from email.message import Message
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import healthcheck
from pydantic import BaseModel


class GmailPayload(BaseModel):
    raw: str


class HealthcheckTests(unittest.TestCase):
    def test_gmail_delivery_refreshes_auth_and_sends_message(self) -> None:
        requests: list[Request] = []

        def api(request: Request, timeout: int) -> io.BytesIO:
            requests.append(request)
            if request.full_url == "https://oauth2.googleapis.com/token":
                assert isinstance(request.data, bytes)
                self.assertIn(b"refresh_token=example", request.data)
                return io.BytesIO(b'{"access_token":"fresh-token"}')
            self.assertEqual(request.get_header("Authorization"), "Bearer fresh-token")
            if request.full_url.endswith("/profile"):
                return io.BytesIO(b'{"emailAddress":"sender@example.com"}')
            assert isinstance(request.data, bytes)
            payload = GmailPayload.model_validate_json(request.data)
            message = base64.urlsafe_b64decode(payload.raw).decode()
            self.assertIn("From: sender@example.com", message)
            self.assertIn("To: recipient@example.com", message)
            self.assertNotIn("fresh-token", message)
            return io.BytesIO(b'{"id":"sent"}')

        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials.json"
            credentials.write_text(
                '{"refresh_token":"example","client_id":"id","client_secret":"secret"}'
            )
            with patch("healthcheck.urlopen", new=api):
                healthcheck.send_email(
                    "recipient@example.com",
                    "/missing/sendmail",
                    "needs-login",
                    gmail_credentials=credentials,
                )
            self.assertEqual(len(requests), 3)

    def test_alert_once_then_rearm_after_recovery(self) -> None:
        sent: list[str] = []

        def send(recipient: str, executable: str, reason: str) -> None:
            sent.append(reason)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            target = Path(directory) / "target.json"
            with patch("healthcheck.send_email", new=send):
                for result in ["needs-login", "needs-login", "healthy", "needs-login"]:

                    def probe(path: Path) -> str:
                        return result

                    with patch("healthcheck.probe", new=probe):
                        healthcheck.check(target, state, "test@example.com", "fake")
            self.assertTrue(sent == ["needs-login", "needs-login"])

    def test_transient_failure_requires_three_checks(self) -> None:
        sent: list[str] = []

        def send(recipient: str, executable: str, reason: str) -> None:
            sent.append(reason)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with patch("healthcheck.send_email", new=send):

                def probe(path: Path) -> str:
                    return "unavailable"

                with patch("healthcheck.probe", new=probe):
                    for _ in range(2):
                        healthcheck.check(state, state, "test@example.com", "fake")
                    self.assertFalse(sent)
                    healthcheck.check(state, state, "test@example.com", "fake")
                    self.assertTrue(sent == ["unavailable"])

    def test_mail_failure_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"

            def probe(path: Path) -> str:
                return "needs-login"

            with patch("healthcheck.probe", new=probe):
                for _ in range(2):
                    with self.assertRaises(subprocess.CalledProcessError):
                        healthcheck.check(
                            state, state, "test@example.com", "/usr/bin/false"
                        )
                    self.assertFalse(state.exists())

    def test_probe_handles_healthy_and_rejected_auth(self) -> None:
        def healthy(request: Request, timeout: int) -> io.BytesIO:
            self.assertEqual(request.get_header("Authorization"), "Bearer example")
            return io.BytesIO(b'{"state":"ready"}')

        def rejected(request: Request, timeout: int) -> io.BytesIO:
            headers: Message[str, str] = Message()
            raise HTTPError(request.full_url, 401, "Unauthorized", headers, None)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            target.write_text(
                '{"baseURL":"http://127.0.0.1:23374","auth":{"accessToken":"example"}}'
            )
            with patch("healthcheck.urlopen", new=healthy):
                self.assertEqual(healthcheck.probe(target), "healthy")
            with patch("healthcheck.urlopen", new=rejected):
                self.assertEqual(healthcheck.probe(target), "authentication rejected")


if __name__ == "__main__":
    unittest.main()
