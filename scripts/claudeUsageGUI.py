#!/home/jdn/Code/ClaudeTools/venv/bin/python
"""
Claude Usage Monitor — graphical rate limit status display.
Refreshes every 5 minutes; click Refresh to update immediately.
"""

import getpass
import json
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import httpx

REFRESH_SEC = 300

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
KEYCHAIN_SERVICE = "Claude Code-credentials"
API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS = {
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

# ── colours ──────────────────────────────────────────────────────────────────
BG     = "#1E1E2E"
CARD   = "#2A2A3E"
FG     = "#CDD6F4"
MUTED  = "#585B70"
TRACK  = "#313244"
GREEN  = "#A6E3A1"
YELLOW = "#F9E2AF"
RED    = "#F38BA8"


def _status_color(s: str) -> str:
    return {"allowed": GREEN, "throttled": YELLOW, "blocked": RED}.get(s, MUTED)


def _pct_color(p: int) -> str:
    if p < 50:
        return GREEN
    if p < 80:
        return YELLOW
    return RED


def _fmt_minutes(m: int) -> str:
    if m <= 0:
        return "now"
    d, r = divmod(m, 1440)
    h, mn = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if mn or not parts:
        parts.append(f"{mn}m")
    return " ".join(parts)


# ── token helpers (same logic as claudeUsage.py) ──────────────────────────
def _extract_token(blob: str) -> str | None:
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token_keychain() -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", getpass.getuser(), "-w"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return _extract_token(out.stdout)
    except Exception:
        return None


def _read_token_file() -> str | None:
    try:
        return _extract_token(CREDENTIALS_PATH.read_text())
    except OSError:
        return None


def read_token() -> str | None:
    return _read_token_keychain() if sys.platform == "darwin" else _read_token_file()


# ── API polling ───────────────────────────────────────────────────────────
def poll_api(token: str) -> dict | None:
    hdrs = {**API_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=20.0) as http:
            resp = http.post(API_URL, headers=hdrs, json=API_BODY)
    except httpx.HTTPError:
        return None

    def hdr(name, default="0"):
        return resp.headers.get(name, default)

    now = time.time()

    def reset_min(ts):
        try:
            mins = (float(ts) - now) / 60.0
            return int(round(mins)) if mins > 0 else 0
        except ValueError:
            return 0

    def pct(util):
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    return {
        "status":        hdr("anthropic-ratelimit-unified-status", "unknown"),
        "active_window": hdr("anthropic-ratelimit-unified-representative-claim", "unknown"),
        "overage_status":hdr("anthropic-ratelimit-unified-overage-status", "unknown"),
        "5h_pct":        pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "5h_reset_min":  reset_min(hdr("anthropic-ratelimit-unified-5h-reset")),
        "5h_status":     hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "7d_pct":        pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "7d_reset_min":  reset_min(hdr("anthropic-ratelimit-unified-7d-reset")),
        "7d_status":     hdr("anthropic-ratelimit-unified-7d-status", "unknown"),
    }


# ── GUI ───────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Claude Usage")
        self.resizable(False, False)
        self.configure(bg=BG)
        self._token = read_token()
        self._next_at = 0
        self._tick_id = None
        self._refresh_id = None
        self._build()
        self._schedule_refresh(delay_ms=0)
        self._tick()

    # ── layout ───────────────────────────────────────────────────────────
    def _build(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(padx=24, pady=20, fill="both")

        # title
        tk.Label(outer, text="Claude Usage Monitor", bg=BG, fg=FG,
                 font=("Sans", 13, "bold")).pack(anchor="w")

        # overall status row
        row = tk.Frame(outer, bg=BG)
        row.pack(anchor="w", pady=(6, 0))
        tk.Label(row, text="Status", bg=BG, fg=MUTED, font=("Sans", 9)).pack(side="left")
        self._status_dot = tk.Label(row, text=" ●", bg=BG, fg=MUTED, font=("Sans", 11))
        self._status_dot.pack(side="left")
        self._status_lbl = tk.Label(row, text="—", bg=BG, fg=FG, font=("Sans", 9))
        self._status_lbl.pack(side="left", padx=(3, 0))

        self._window_lbl = tk.Label(outer, text="Active window: —", bg=BG, fg=MUTED,
                                    font=("Sans", 9))
        self._window_lbl.pack(anchor="w")

        # window cards
        self._cards = {}
        for key, title in (("5h", "5-Hour Window"), ("7d", "7-Day Window")):
            self._cards[key] = self._make_card(outer, key, title)

        # footer
        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", pady=(14, 0))

        self._overage_lbl = tk.Label(foot, text="Overage: —", bg=BG, fg=MUTED,
                                     font=("Sans", 9))
        self._overage_lbl.pack(side="left")

        self._countdown_lbl = tk.Label(foot, text="", bg=BG, fg=MUTED, font=("Sans", 9))
        self._countdown_lbl.pack(side="right")

        btn = tk.Button(foot, text="⟳  Refresh", bg=CARD, fg=FG, font=("Sans", 9),
                        relief="flat", padx=8, pady=3, cursor="hand2",
                        activebackground="#3A3A5E", activeforeground=FG,
                        command=lambda: self._schedule_refresh(delay_ms=0))
        btn.pack(side="right", padx=(0, 10))

    def _make_card(self, parent, key, title):
        card = tk.Frame(parent, bg=CARD, padx=14, pady=10)
        card.pack(fill="x", pady=(12, 0))

        hdr = tk.Frame(card, bg=CARD)
        hdr.pack(fill="x")
        tk.Label(hdr, text=title, bg=CARD, fg=FG, font=("Sans", 10, "bold")).pack(side="left")
        pct_lbl = tk.Label(hdr, text="—", bg=CARD, fg=MUTED, font=("Sans", 10, "bold"))
        pct_lbl.pack(side="right")

        canvas = tk.Canvas(card, height=8, bg=TRACK, highlightthickness=0, width=320)
        canvas.pack(fill="x", pady=(8, 4))
        bar = canvas.create_rectangle(0, 0, 0, 8, fill=GREEN, width=0)

        reset_lbl = tk.Label(card, text="Resets in: —", bg=CARD, fg=MUTED, font=("Sans", 9))
        reset_lbl.pack(anchor="w")

        return {"pct_lbl": pct_lbl, "canvas": canvas, "bar": bar, "reset_lbl": reset_lbl}

    # ── data application ─────────────────────────────────────────────────
    def _apply(self, data: dict):
        s = data.get("status", "unknown")
        self._status_dot.config(fg=_status_color(s))
        self._status_lbl.config(text=s)

        aw = data.get("active_window", "unknown").replace("_", " ")
        self._window_lbl.config(text=f"Active window: {aw}")

        for key in ("5h", "7d"):
            p   = data.get(f"{key}_pct", 0)
            rm  = data.get(f"{key}_reset_min", 0)
            c   = self._cards[key]

            c["pct_lbl"].config(text=f"{p}%", fg=_pct_color(p))
            c["reset_lbl"].config(text=f"Resets in: {_fmt_minutes(rm)}")

            canvas = c["canvas"]
            canvas.update_idletasks()
            w = canvas.winfo_width() or 320
            canvas.coords(c["bar"], 0, 0, int(w * p / 100), 8)
            canvas.itemconfig(c["bar"], fill=_pct_color(p))

        ov = data.get("overage_status", "unknown")
        self._overage_lbl.config(text=f"Overage: {ov}")

    # ── refresh scheduling ────────────────────────────────────────────────
    def _schedule_refresh(self, delay_ms: int):
        if self._refresh_id:
            self.after_cancel(self._refresh_id)
        self._next_at = time.time() + delay_ms / 1000
        self._countdown_lbl.config(text="Fetching…")
        self._refresh_id = self.after(delay_ms, self._do_refresh)

    def _do_refresh(self):
        self._next_at = time.time() + REFRESH_SEC

        def fetch():
            if not self._token:
                self._token = read_token()
            if self._token:
                data = poll_api(self._token)
                if data:
                    self.after(0, lambda: self._apply(data))
                else:
                    self.after(0, lambda: self._countdown_lbl.config(text="API error"))
            else:
                self.after(0, lambda: self._countdown_lbl.config(text="No credentials"))
            self.after(0, lambda: self._schedule_refresh(delay_ms=REFRESH_SEC * 1000))

        threading.Thread(target=fetch, daemon=True).start()

    # ── per-second countdown ──────────────────────────────────────────────
    def _tick(self):
        remaining = max(0, int(self._next_at - time.time()))
        if remaining > 0:
            m, s = divmod(remaining, 60)
            self._countdown_lbl.config(text=f"Next refresh in {m}:{s:02d}")
        self._tick_id = self.after(1000, self._tick)


if __name__ == "__main__":
    App().mainloop()
