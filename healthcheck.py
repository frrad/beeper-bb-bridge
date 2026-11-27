"""Periodic Beeper readiness probe with deduplicated sendmail alerts."""

from __future__ import annotations

import argparse
import base64
import fcntl
import logging
import os
import socket
import subprocess
import json
from http.client import HTTPResponse
from typing import cast
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError

LOG = logging.getLogger("beeper-healthcheck")


class Auth(BaseModel):
    accessToken: str


class Target(BaseModel):
    baseURL: str
    auth: Auth | None = None


class Readiness(BaseModel):
    state: str


class AlertState(BaseModel):
    failures: int = 0
    notified: bool = False


class GmailCredentials(BaseModel):
    refresh_token: str
    client_id: str
    client_secret: str


class OAuthToken(BaseModel):
    access_token: str


class GmailProfile(BaseModel):
    emailAddress: str


def gmail_message(credentials_path: Path, message: bytes) -> None:
    credentials = GmailCredentials.model_validate_json(credentials_path.read_bytes())
    refresh = Request(
        "https://oauth2.googleapis.com/token",
        data=urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            }
        ).encode(),
    )
    with cast(HTTPResponse, urlopen(refresh, timeout=15)) as response:
        token = OAuthToken.model_validate_json(response.read()).access_token
    headers = {"Authorization": f"Bearer {token}"}
    profile_request = Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers
    )
    with cast(HTTPResponse, urlopen(profile_request, timeout=15)) as response:
        sender = GmailProfile.model_validate_json(response.read()).emailAddress
    if "\r" in sender or "\n" in sender:
        raise ValueError("Invalid sender")
    raw = base64.urlsafe_b64encode(f"From: {sender}\n".encode() + message).decode()
    payload: dict[str, str] = {"raw": raw}
    request = Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )
    with cast(HTTPResponse, urlopen(request, timeout=15)) as response:
        response.read()


def probe(target_path: Path) -> str:
    try:
        target = Target.model_validate_json(target_path.read_bytes())
        headers = {}
        if target.auth is not None:
            headers["Authorization"] = f"Bearer {target.auth.accessToken}"
        request = Request(target.baseURL.rstrip("/") + "/v1/app/setup", headers=headers)
        with cast(HTTPResponse, urlopen(request, timeout=15)) as response:
            readiness = Readiness.model_validate_json(response.read())
        if readiness.state in {"needs-login", "needs-verification", "needs-secrets"}:
            return readiness.state
        if readiness.state in {"ready", "initializing", "needs-first-sync"}:
            return "healthy"
        return "unavailable"
    except HTTPError as error:
        return "authentication rejected" if error.code in {401, 403} else "unavailable"
    except (OSError, URLError, ValidationError, ValueError):
        # Do not log response bodies or config: they can contain tokens or keys.
        return "unavailable"


def send_email(
    recipient: str,
    sendmail: str,
    reason: str,
    *,
    test: bool = False,
    gmail_credentials: Path | None = None,
) -> None:
    if "\r" in recipient or "\n" in recipient:
        raise ValueError("Invalid recipient")
    subject = "Beeper watcher: test alert" if test else "Beeper watcher needs attention"
    body = (
        f"Beeper watcher on {socket.gethostname()}: {reason}.\n\n"
        "The @bb bridge may be missing new messages. Open bb and ask an agent to "
        "check Beeper authentication and restart the watcher.\n"
        "For sign-in, use beeper auth email start --target server --email YOUR_EMAIL, "
        "then complete device verification if requested.\n\n"
        "Resend missed @bb commands after recovery; there is no history catch-up.\n"
        "This monitor sends one alert per outage and rearms after recovery.\n"
    )
    message = (
        f"To: {recipient}\nSubject: {subject}\nContent-Type: text/plain; charset=utf-8\n\n"
        + body
    ).encode("utf-8")
    if gmail_credentials is not None:
        gmail_message(gmail_credentials, message)
        return
    subprocess.run(
        [sendmail, "-t", "-oi"],
        input=message,
        check=True,
        timeout=45,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def check(
    target: Path,
    state_path: Path,
    recipient: str,
    sendmail: str,
    gmail_credentials: Path | None = None,
) -> None:
    state = (
        AlertState.model_validate_json(state_path.read_bytes())
        if state_path.exists()
        else AlertState()
    )
    result = probe(target)
    if result == "healthy":
        if state.notified:
            LOG.info("Beeper recovered; email alert rearmed")
        state = AlertState()
    else:
        state.failures += 1
        # Authentication errors alert immediately. Allow transient server/network
        # failures three consecutive checks before alerting.
        if not state.notified and (result != "unavailable" or state.failures >= 3):
            if gmail_credentials is None:
                send_email(recipient, sendmail, result)
            else:
                send_email(
                    recipient, sendmail, result, gmail_credentials=gmail_credentials
                )
            state.notified = True
            LOG.warning("Beeper health alert sent: %s", result)
        else:
            LOG.warning("Beeper health: %s", result)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(state.model_dump_json() + "\n")
    temporary.replace(state_path)


class Options(argparse.Namespace):
    recipient: str
    target_file: Path
    state: Path
    sendmail: str
    test_email: bool
    gmail_credentials: Path | None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", required=True)
    parser.add_argument(
        "--target-file", type=Path, default=Path.home() / ".beeper/targets/server.json"
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".local/state/beeper-bb-bridge/health.json",
    )
    parser.add_argument("--sendmail", default="/usr/sbin/sendmail")
    parser.add_argument(
        "--gmail-credentials",
        type=Path,
        help="Existing Gmail OAuth credentials JSON; uses Gmail instead of sendmail",
    )
    parser.add_argument("--test-email", action="store_true")
    args = parser.parse_args(namespace=Options())
    recipient = str(args.recipient)
    target = Path(args.target_file)
    state = Path(args.state)
    sendmail = str(args.sendmail)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.umask(0o077)
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.test_email:
            send_email(
                recipient,
                sendmail,
                "email delivery test",
                test=True,
                gmail_credentials=args.gmail_credentials,
            )
            LOG.info("Test email accepted by mail transport")
        else:
            check(target, state, recipient, sendmail, args.gmail_credentials)


if __name__ == "__main__":
    main()
