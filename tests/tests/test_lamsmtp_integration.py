#!/usr/bin/env python3
"""Integration test for the ``lamsmtp`` standard library module.

Runs against a MailHog container (http://mailhog.github.io) that
the test starts / stops itself. MailHog is a zero-config mail
catcher with a built-in HTTP API for listing captured messages,
which makes round-trip verification trivial.

Flow::

    1. ``docker run --rm -d mailhog/mailhog:latest`` on ports
       1025 (SMTP) and 8025 (HTTP).
    2. Wait for the SMTP port to accept connections.
    3. Compile + run a tiny Lam program that sends two messages.
    4. Pull ``/api/v2/messages`` from MailHog, assert:
       - one plaintext and one multipart/alternative were captured
       - envelope recipients match (including the Cc in message #2)
       - subject headers survived the round-trip
       - the HTML alternative carries the ``<h1>`` marker
    5. ``docker rm -f`` the container.

Skips cleanly (exit 0, "SKIP" marker) when docker isn't available
so developers without docker can still run the non-networked
suites.

Run with::

    python3 tests/tests/test_lamsmtp_integration.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]

CONTAINER = "lamsmtp_mh_test"
IMAGE = "mailhog/mailhog:latest"
SMTP_PORT = 1025
HTTP_PORT = 8025
HTTP_BASE = f"http://localhost:{HTTP_PORT}"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Poll ``host:port`` until it accepts a TCP connection, up to
    ``timeout`` seconds. Returns ``True`` if the port became ready,
    ``False`` on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_mailhog() -> None:
    # Remove any stale container from a previous failed run so we
    # don't crash on "name already in use".
    subprocess.run(
        ["docker", "rm", "-f", CONTAINER],
        capture_output=True, check=False,
    )
    subprocess.run(
        [
            "docker", "run", "--rm", "-d",
            "--name", CONTAINER,
            "-p", f"{SMTP_PORT}:1025",
            "-p", f"{HTTP_PORT}:8025",
            IMAGE,
        ],
        capture_output=True, text=True, check=True,
    )


def _stop_mailhog() -> None:
    subprocess.run(
        ["docker", "rm", "-f", CONTAINER],
        capture_output=True, check=False,
    )


def _purge_messages() -> None:
    req = urllib.request.Request(
        f"{HTTP_BASE}/api/v1/messages",
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req, timeout=5.0).read()
    except urllib.error.URLError:
        pass


def _fetch_messages() -> list[dict]:
    with urllib.request.urlopen(
        f"{HTTP_BASE}/api/v2/messages", timeout=5.0,
    ) as r:
        data = json.loads(r.read())
    return data.get("items", [])


def _fetch_message_source(msg_id: str) -> str:
    """MailHog's raw-source endpoint returns the complete RFC 5322
    blob as a string, which lets us grep for specific headers and
    MIME parts without duplicating the body across multiple fields
    of the JSON representation."""
    with urllib.request.urlopen(
        f"{HTTP_BASE}/api/v1/messages/{msg_id}", timeout=5.0,
    ) as r:
        data = json.loads(r.read())
    headers = data.get("Raw", {}).get("Data") or ""
    # Some builds return the raw source under ``Content.Body``; fall
    # back so we don't care which one the server uses.
    if not headers:
        headers = data.get("Content", {}).get("Body", "")
    return headers


def _write_program(tmp: Path) -> Path:
    src = tmp / "lamsmtp_send.lam"
    src.write_text(
        "from lamsmtp import Smtp, Mail\n"
        "\n"
        "func main() {\n"
        f'    Smtp.sendMail(\n'
        f'        host="localhost:{SMTP_PORT}",\n'
        '        sender="alice@example.com",\n'
        '        to="bob@example.com",\n'
        '        subject="Plain regression",\n'
        '        text="Hi Bob, this is plaintext.",\n'
        "    )\n"
        "    print(\"sent-plain: ok\")\n"
        "\n"
        "    m: Mail = Mail()\n"
        '    m.setSender("alice@example.com")\n'
        '    m.addTo("bob@example.com")\n'
        '    m.addTo("carol@example.com")\n'
        '    m.addCc("dave@example.com")\n'
        '    m.setSubject("Multipart regression")\n'
        '    m.setText("Plain fallback body.")\n'
        '    m.setHtml("<h1>Rich body</h1><p>With <em>HTML</em>.</p>")\n'
        '    m.setHeader("X-Mailer", "lamsmtp-test/0.1")\n'
        f'    Smtp.send(host="localhost:{SMTP_PORT}", mail=m)\n'
        "    print(\"sent-builder: ok\")\n"
        "}\n",
        encoding="utf-8",
    )
    return src


def _run_lam(src: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        LAMC + ["--run", str(src)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )


def _addrs(msg: dict, field: str) -> list[str]:
    return [f"{p['Mailbox']}@{p['Domain']}" for p in msg.get(field, [])]


def _subject(msg: dict) -> str:
    return msg.get("Content", {}).get("Headers", {}).get("Subject", [""])[0]


def _content_type(msg: dict) -> str:
    return (
        msg.get("Content", {}).get("Headers", {}).get("Content-Type", [""])[0]
    )


def main() -> int:
    if not _docker_available():
        print("SKIP: docker not found on PATH; "
              "lamsmtp integration test needs MailHog")
        return 0

    _start_mailhog()
    try:
        if not _wait_for_port("localhost", SMTP_PORT, timeout=30.0):
            print("FAIL: MailHog SMTP :1025 never came up")
            return 1
        if not _wait_for_port("localhost", HTTP_PORT, timeout=30.0):
            print("FAIL: MailHog HTTP :8025 never came up")
            return 1

        _purge_messages()

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = _write_program(Path(tmp))
            proc = _run_lam(src)

        if proc.returncode != 0:
            print(f"FAIL: lamc run: rc={proc.returncode}")
            print(f"stdout: {proc.stdout}")
            print(f"stderr: {proc.stderr}")
            return 1

        stdout_lines = proc.stdout.strip().splitlines()
        if stdout_lines != ["sent-plain: ok", "sent-builder: ok"]:
            print(f"FAIL: unexpected program output: {stdout_lines!r}")
            return 1
        print("PASS: lamsmtp compile + send")

        # MailHog commits messages asynchronously; the default poll
        # loop gives it up to 2s to catch up.
        msgs: list[dict] = []
        deadline = time.time() + 2.0
        while time.time() < deadline:
            msgs = _fetch_messages()
            if len(msgs) >= 2:
                break
            time.sleep(0.1)

        if len(msgs) != 2:
            print(f"FAIL: expected 2 captured messages, got {len(msgs)}")
            print(json.dumps(msgs, indent=2))
            return 1
        print("PASS: MailHog captured both messages")

        # Items come back newest-first. Find them by subject.
        by_subject = { _subject(m): m for m in msgs }
        if "Plain regression" not in by_subject:
            print("FAIL: plaintext message not received")
            return 1
        if "Multipart regression" not in by_subject:
            print("FAIL: multipart message not received")
            return 1
        print("PASS: both subjects present after round-trip")

        plain = by_subject["Plain regression"]
        if _addrs(plain, "To") != ["bob@example.com"]:
            print(f"FAIL: plain recipients wrong: {_addrs(plain, 'To')}")
            return 1
        if not _content_type(plain).startswith("text/plain"):
            print(f"FAIL: plain content-type wrong: {_content_type(plain)}")
            return 1
        print("PASS: plaintext headers intact")

        multi = by_subject["Multipart regression"]
        to_addrs = sorted(_addrs(multi, "To"))
        # ``To`` in MailHog's envelope view lumps every RCPT TO
        # together — To, Cc, Bcc — because SMTP doesn't distinguish
        # after the handshake. That's exactly the behaviour we want
        # to assert on.
        expected_envelope = sorted([
            "bob@example.com", "carol@example.com", "dave@example.com",
        ])
        if to_addrs != expected_envelope:
            print(f"FAIL: multipart envelope wrong: {to_addrs} "
                  f"(expected {expected_envelope})")
            return 1
        if "multipart/alternative" not in _content_type(multi):
            print(f"FAIL: multipart content-type wrong: "
                  f"{_content_type(multi)}")
            return 1
        print("PASS: multipart envelope + content-type intact")

        # Verify the HTML alternative survived — grep the raw body.
        raw_body = multi.get("Content", {}).get("Body", "")
        if "<h1>Rich body</h1>" not in raw_body:
            # Body as returned is the multipart blob with boundaries;
            # the HTML part should be visible verbatim since we pass
            # 8-bit encoding.
            print(f"FAIL: HTML part missing from body\n{raw_body[:500]}")
            return 1
        if "Plain fallback body." not in raw_body:
            print(f"FAIL: plaintext alternative missing from body")
            return 1
        print("PASS: HTML + plaintext alternatives both present")

        # Custom header round-trip.
        xm = multi.get("Content", {}).get("Headers", {}).get(
            "X-Mailer", []
        )
        if xm != ["lamsmtp-test/0.1"]:
            print(f"FAIL: X-Mailer header wrong: {xm}")
            return 1
        print("PASS: custom headers round-tripped")

        print()
        print("=" * 60)
        print("lamsmtp integration: all checks passed")
        return 0
    finally:
        _stop_mailhog()


if __name__ == "__main__":
    sys.exit(main())
