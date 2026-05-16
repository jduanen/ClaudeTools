#!/home/jdn/Code/ClaudeTools/venv/bin/python
"""
Claude Usage Monitor — concentric dial gauges.
Outer ring = 5-hour window, inner ring = 7-day window.
Zones: green (0–75 %), amber (75–90 %), red (90–100 %).
"""

import getpass, json, re, subprocess, sys, threading, time, tkinter as tk
from pathlib import Path
import httpx

REFRESH_SEC = 300
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
KEYCHAIN_SERVICE  = "Claude Code-credentials"
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

BG      = "#1E1E2E"
FG      = "#CDD6F4"
MUTED   = "#585B70"
GREEN   = "#A6E3A1"
AMBER   = "#F9E2AF"
RED     = "#F38BA8"
DIM_GRN = "#1D3D1D"
DIM_AMB = "#3D2D0D"
DIM_RED = "#3D1018"

# Gauge geometry
SWEEP     = 240   # degrees of arc sweep
START_DEG = 210   # start angle (tkinter: CCW from East = 7-o'clock on screen)
GRN_PCT   = 75
AMB_PCT   = 90

# Canvas / ring dimensions
CW, CH  = 320, 240
CX, CY  = 160, 140   # arc centre (leaves 15 px above arc top, ~40 px below endpoints)
R_OUT   = 125        # outer ring radius  (5-hour)
R_IN    = 84         # inner ring radius  (7-day)
ARC_W   = 26         # stroke width for both rings


def _status_color(s):
    return {"allowed": GREEN, "throttled": AMBER, "blocked": RED}.get(s, MUTED)


def _pct_color(p):
    return GREEN if p < GRN_PCT else (AMBER if p < AMB_PCT else RED)


def _fmt_minutes(m):
    if m <= 0:
        return "now"
    d, r = divmod(m, 1440)
    h, mn = divmod(r, 60)
    parts = []
    if d:  parts.append(f"{d}d")
    if h:  parts.append(f"{h}h")
    if mn or not parts: parts.append(f"{mn}m")
    return " ".join(parts)


# ── token helpers ─────────────────────────────────────────────────────────────
def _extract_token(blob):
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


def _read_token_keychain():
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", getpass.getuser(), "-w"],
            check=True, capture_output=True, text=True, timeout=10)
        return _extract_token(out.stdout)
    except Exception:
        return None


def _read_token_file():
    try:
        return _extract_token(CREDENTIALS_PATH.read_text())
    except OSError:
        return None


def read_token():
    return _read_token_keychain() if sys.platform == "darwin" else _read_token_file()


# ── API polling ───────────────────────────────────────────────────────────────
def poll_api(token):
    hdrs = {**API_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=20.0) as http:
            resp = http.post(API_URL, headers=hdrs, json=API_BODY)
    except httpx.HTTPError:
        return None

    now = time.time()
    def hdr(n, d="0"):  return resp.headers.get(n, d)
    def rmin(ts):
        try:
            v = (float(ts) - now) / 60
            return int(round(v)) if v > 0 else 0
        except ValueError:
            return 0
    def pct(u):
        try:    return int(round(float(u) * 100))
        except ValueError: return 0

    return {
        "status":        hdr("anthropic-ratelimit-unified-status", "unknown"),
        "active_window": hdr("anthropic-ratelimit-unified-representative-claim", "unknown"),
        "overage_status":hdr("anthropic-ratelimit-unified-overage-status", "unknown"),
        "5h_pct":        pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "5h_reset_min":  rmin(hdr("anthropic-ratelimit-unified-5h-reset")),
        "5h_status":     hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "7d_pct":        pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "7d_reset_min":  rmin(hdr("anthropic-ratelimit-unified-7d-reset")),
        "7d_status":     hdr("anthropic-ratelimit-unified-7d-status", "unknown"),
    }


# ── dial drawing ──────────────────────────────────────────────────────────────
def _draw_ring(canvas, r, pct, tag):
    """Redraw one gauge ring for the given usage percentage."""
    canvas.delete(tag)
    x1, y1, x2, y2 = CX - r, CY - r, CX + r, CY + r

    def angle_at(p):
        return START_DEG - (p / 100) * SWEEP

    def ext(p0, p1):
        return -((p1 - p0) / 100) * SWEEP

    # Dim background zones (full arc)
    for p0, p1, col in [(0, GRN_PCT, DIM_GRN),
                         (GRN_PCT, AMB_PCT, DIM_AMB),
                         (AMB_PCT, 100, DIM_RED)]:
        canvas.create_arc(x1, y1, x2, y2,
                          start=angle_at(p0), extent=ext(p0, p1),
                          style=tk.ARC, outline=col, width=ARC_W, tags=tag)

    if pct <= 0:
        return

    # Bright foreground zones up to current usage
    for p0, p1, col in [(0, GRN_PCT, GREEN),
                         (GRN_PCT, AMB_PCT, AMBER),
                         (AMB_PCT, 100, RED)]:
        if pct <= p0:
            break
        canvas.create_arc(x1, y1, x2, y2,
                          start=angle_at(p0), extent=ext(p0, min(pct, p1)),
                          style=tk.ARC, outline=col, width=ARC_W, tags=tag)


# ── application ───────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Claude Usage")
        self.resizable(False, False)
        self.configure(bg=BG)
        self._token = read_token()
        self._next_at = 0
        self._refresh_id = None
        self._build()
        self._schedule_refresh(delay_ms=0)
        self._tick()

    def _build(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(padx=20, pady=16, fill="both")

        tk.Label(outer, text="Claude Usage Monitor", bg=BG, fg=FG,
                 font=("Sans", 13, "bold")).pack(anchor="w")

        self._canvas = tk.Canvas(outer, width=CW, height=CH, bg=BG, highlightthickness=0)
        self._canvas.pack(pady=(10, 0))

        # Ring labels near the right-side arc endpoints (at the 330° position)
        # Outer ring endpoint: x≈268, y≈203  — label just right of it
        # Inner ring endpoint: x≈233, y≈183  — label just right of it
        self._canvas.create_text(CX + R_OUT + 12, CY + int(R_OUT * 0.5) + 4,
                                  text="5H", fill=MUTED, font=("Sans", 8), anchor="w")
        self._canvas.create_text(CX + R_IN + 12, CY + int(R_IN * 0.5) + 4,
                                  text="7D", fill=MUTED, font=("Sans", 8), anchor="w")

        # Centre percentage readouts (sit inside the inner ring)
        self._t5h = self._canvas.create_text(
            CX, CY - 14, text="5H  —", fill=MUTED, font=("Mono", 12, "bold"))
        self._t7d = self._canvas.create_text(
            CX, CY + 12, text="7D  —", fill=MUTED, font=("Mono", 12, "bold"))

        # Status row
        row = tk.Frame(outer, bg=BG)
        row.pack(anchor="w", pady=(10, 0))
        tk.Label(row, text="Status", bg=BG, fg=MUTED, font=("Sans", 9)).pack(side="left")
        self._sdot = tk.Label(row, text=" ●", bg=BG, fg=MUTED, font=("Sans", 11))
        self._sdot.pack(side="left")
        self._slbl = tk.Label(row, text="—", bg=BG, fg=FG, font=("Sans", 9))
        self._slbl.pack(side="left", padx=(3, 0))

        self._wlbl = tk.Label(outer, text="Active window: —", bg=BG, fg=MUTED,
                               font=("Sans", 9))
        self._wlbl.pack(anchor="w")

        self._r5h = tk.Label(outer, text="5H resets in: —", bg=BG, fg=MUTED,
                              font=("Sans", 9))
        self._r5h.pack(anchor="w")
        self._r7d = tk.Label(outer, text="7D resets in: —", bg=BG, fg=MUTED,
                              font=("Sans", 9))
        self._r7d.pack(anchor="w")

        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", pady=(10, 0))

        self._ovlbl = tk.Label(foot, text="Overage: —", bg=BG, fg=MUTED, font=("Sans", 9))
        self._ovlbl.pack(side="left")

        self._cdlbl = tk.Label(foot, text="", bg=BG, fg=MUTED, font=("Sans", 9))
        self._cdlbl.pack(side="right")

        tk.Button(foot, text="⟳  Refresh", bg="#2A2A3E", fg=FG, font=("Sans", 9),
                  relief="flat", padx=8, pady=3, cursor="hand2",
                  activebackground="#3A3A5E", activeforeground=FG,
                  command=lambda: self._schedule_refresh(delay_ms=0)
                  ).pack(side="right", padx=(0, 10))

        _draw_ring(self._canvas, R_OUT, 0, "ring5h")
        _draw_ring(self._canvas, R_IN,  0, "ring7d")

    def _apply(self, data):
        s = data.get("status", "unknown")
        self._sdot.config(fg=_status_color(s))
        self._slbl.config(text=s)
        self._wlbl.config(
            text="Active window: " + data.get("active_window", "—").replace("_", " "))

        p5 = data.get("5h_pct", 0)
        p7 = data.get("7d_pct", 0)

        _draw_ring(self._canvas, R_OUT, p5, "ring5h")
        _draw_ring(self._canvas, R_IN,  p7, "ring7d")

        self._canvas.itemconfig(self._t5h, text=f"5H  {p5:>3}%", fill=_pct_color(p5))
        self._canvas.itemconfig(self._t7d, text=f"7D  {p7:>3}%", fill=_pct_color(p7))

        self._r5h.config(text=f"5H resets in: {_fmt_minutes(data.get('5h_reset_min', 0))}")
        self._r7d.config(text=f"7D resets in: {_fmt_minutes(data.get('7d_reset_min', 0))}")
        self._ovlbl.config(text="Overage: " + data.get("overage_status", "—"))

    def _schedule_refresh(self, delay_ms):
        if self._refresh_id:
            self.after_cancel(self._refresh_id)
        self._next_at = time.time() + delay_ms / 1000
        self._cdlbl.config(text="Fetching…")
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
                    self.after(0, lambda: self._cdlbl.config(text="API error"))
            else:
                self.after(0, lambda: self._cdlbl.config(text="No credentials"))
            self.after(0, lambda: self._schedule_refresh(delay_ms=REFRESH_SEC * 1000))

        threading.Thread(target=fetch, daemon=True).start()

    def _tick(self):
        remaining = max(0, int(self._next_at - time.time()))
        if remaining > 0:
            m, s = divmod(remaining, 60)
            self._cdlbl.config(text=f"Next refresh in {m}:{s:02d}")
        self.after(1000, self._tick)


if __name__ == "__main__":
    App().mainloop()
