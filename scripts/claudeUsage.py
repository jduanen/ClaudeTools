#!/home/jdn/Code/ClaudeTools/venv/bin/python
"""
Script to get current Claude usage information

"""

import getpass
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx


LOG_LEVEL = logging.WARNING

# Claude credentials in ~/.claude/.credentials.json
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # raw token (no JSON wrapper), must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None

def _read_token_keychain() -> str | None:
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log.warning(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning(f"Keychain access error: {e}")
        return None
    return _extract_access_token(out.stdout)


def _read_token_file() -> str | None:
    try:
        raw = CREDENTIALS_PATH.read_text()
    except OSError as e:
        log.warning(f"Error reading credentials: {e}")
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    if sys.platform == "darwin":
        return _read_token_keychain()
    return _read_token_file()


def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=20.0) as http:
            resp = http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        log.warning(f"API call failed: {e}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    payload = {
        "status": hdr("anthropic-ratelimit-unified-status", "unknown"),
        "active_window": hdr("anthropic-ratelimit-unified-representative-claim", "unknown"),
        "overage_status": hdr("anthropic-ratelimit-unified-overage-status", "unknown"),
        "5h_pct": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "5h_reset_min": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
        "5h_status": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "7d_pct": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "7d_reset_min": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
        "7d_status": hdr("anthropic-ratelimit-unified-7d-status", "unknown"),
        "ok": True,
    }
    return payload


def main() -> None:
    token = read_token()
    if not token:
        logging.error("Failed to get token")
        sys.exit(1)
    payload = poll_api(token)
    if payload is not None:
        json.dump(payload, sys.stdout, indent=4, sort_keys=False)
        print("")


if __name__ == "__main__":
    main()
    sys.exit(0)
