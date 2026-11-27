"""Exercise outage/recovery transitions and delivery failures without real mail."""

from __future__ import annotations

import io
from email.message import Message
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import healthcheck


class HealthcheckTests(unittest.TestCase):
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
