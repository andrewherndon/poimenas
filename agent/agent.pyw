"""
Windows Diagnostic Core Service
System health monitoring and telemetry agent
"""
from __future__ import annotations
import sys, json, time, threading, logging
from pathlib import Path
from datetime import date

import requests
import psutil
import win32api
import win32gui
import win32process
import tkinter as tk
from tkinter import font as tkfont

# ── Config ────────────────────────────────────────────────────────────────────

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

with open(BASE / "config.json") as f:
    cfg = json.load(f)

SERVER        = cfg["server_url"].rstrip("/")
KEY           = cfg["api_key"]
BLOCKED_PROCS = {p.lower() for p in cfg.get("blocked_processes", [])}
WEB_TARGETS   = cfg.get("web_targets", {
    "seterra":  ["seterra.com", "seterra"],
    "duolingo": ["duolingo.com", "duolingo"],
})
VERSION = "0.1.0"
IDLE_CUTOFF = 30   # seconds without input before we stop counting web time
POLL_SECS   = 30   # server poll interval

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

logging.basicConfig(
    filename=str(BASE / "diag.log"),
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────

_mu = threading.Lock()
_state: dict = dict(
    locked=True, reason="", server_ok=False, messages=[],
    anki_cards=0,    anki_target=0,
    seterra_secs=0,  seterra_target=0,
    duolingo_secs=0, duolingo_target=0,
    gaming_secs=0,   gaming_cap=None,
)

def gs(k):
    with _mu: return _state[k]

def ss(**kw):
    with _mu: _state.update(kw)

def snap():
    with _mu: return dict(_state)

# ── Daily counters (reset at midnight) ───────────────────────────────────────

_cnt_mu   = threading.Lock()
_day      = date.today().isoformat()
_seterra  = 0
_duolingo = 0
_gaming   = 0

def _maybe_reset():
    global _day, _seterra, _duolingo, _gaming
    with _cnt_mu:
        today = date.today().isoformat()
        if today != _day:
            _day, _seterra, _duolingo, _gaming = today, 0, 0, 0

def _counts():
    with _cnt_mu:
        return _seterra, _duolingo, _gaming

# ── Windows helpers ───────────────────────────────────────────────────────────

def idle_secs() -> float:
    last = win32api.GetLastInputInfo()
    tick = win32api.GetTickCount()
    return (tick - last) / 1000.0

def fg_browser_title() -> str:
    """Return lowercase window title if the foreground window is a browser."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        name = psutil.Process(pid).name().lower()
        if any(b in name for b in ("chrome", "msedge", "firefox", "opera", "brave")):
            return win32gui.GetWindowText(hwnd).lower()
    except Exception:
        pass
    return ""

def blocked_running() -> bool:
    try:
        names = {p.name().lower() for p in psutil.process_iter(["name"])}
        return bool(names & BLOCKED_PROCS)
    except Exception:
        return False

def suspend_blocked():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"].lower() in BLOCKED_PROCS:
                p.suspend()
        except Exception:
            pass

def resume_blocked():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"].lower() in BLOCKED_PROCS:
                p.resume()
        except Exception:
            pass

def anki_today() -> int:
    try:
        r = requests.post(
            "http://localhost:8765",
            json={"action": "getNumCardsReviewedToday", "version": 6},
            timeout=2,
        )
        return int(r.json().get("result") or 0)
    except Exception:
        return 0

def fmt_time(secs: int) -> str:
    if secs <= 0:   return "0s"
    if secs < 60:   return f"{secs}s"
    if secs < 3600: return f"{secs // 60}m"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h}h {m}m" if m else f"{h}h"

# ── Tick loop — 1 s, tracks active time ──────────────────────────────────────

def tick_loop():
    global _seterra, _duolingo, _gaming
    while True:
        time.sleep(1)
        _maybe_reset()
        if idle_secs() > IDLE_CUTOFF:
            continue
        title = fg_browser_title()
        with _cnt_mu:
            if any(kw in title for kw in WEB_TARGETS.get("seterra", [])):
                _seterra += 1
            elif any(kw in title for kw in WEB_TARGETS.get("duolingo", [])):
                _duolingo += 1
            if not gs("locked") and blocked_running():
                _gaming += 1

# ── Poll loop — 30 s, talks to server ────────────────────────────────────────

def poll_loop():
    was_locked = True
    while True:
        _maybe_reset()
        anki = anki_today()
        s, d, g = _counts()

        try:
            status = requests.get(
                f"{SERVER}/api/status", headers=HEADERS, timeout=8
            ).json()
            t      = status.get("today", {})
            locked = bool(status.get("locked", True))

            ss(
                locked=locked,
                reason=status.get("reason", ""),
                server_ok=True,
                messages=status.get("pending_messages", []),
                anki_cards=anki,    anki_target=t.get("anki_target", 0),
                seterra_secs=s,     seterra_target=t.get("seterra_target_seconds", 0),
                duolingo_secs=d,    duolingo_target=t.get("duolingo_target_seconds", 0),
                gaming_secs=g,      gaming_cap=t.get("gaming_cap_seconds"),
            )

            if locked and not was_locked:
                suspend_blocked()
            elif not locked and was_locked:
                resume_blocked()
            was_locked = locked

        except Exception as e:
            log.warning("Status poll failed: %s", e)
            ss(server_ok=False)

        try:
            requests.post(
                f"{SERVER}/api/heartbeat", headers=HEADERS, timeout=8,
                json={
                    "agent_version": VERSION,
                    "anki_cards": anki,
                    "seterra_active_seconds": s,
                    "duolingo_active_seconds": d,
                    "gaming_seconds": g,
                },
            )
        except Exception as e:
            log.warning("Heartbeat failed: %s", e)

        time.sleep(POLL_SECS)

# ── Overlay ───────────────────────────────────────────────────────────────────

BG   = "#0d0000"
FG   = "#FAF7F0"
GOLD = "#D4AF37"
RED  = "#ef4444"
GRN  = "#4ade80"
DIM  = "#666666"

REASON_TEXT = {
    "prerequisite": "Finish your daily tasks to unlock games",
    "cap_exceeded":  "Daily screen time limit reached",
    "manual":        "Computer locked by Andrew",
    "":              "Locked",
}

class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self._visible = False
        self._build()
        self.root.after(1000, self._tick)

    def _build(self):
        r = self.root
        r.title("Windows Diagnostic Core")
        r.configure(bg=BG)
        r.attributes("-fullscreen", True)
        r.attributes("-topmost", True)
        r.protocol("WM_DELETE_WINDOW", lambda: None)
        r.bind("<Alt-F4>", lambda e: "break")
        r.bind("<Escape>",  lambda e: "break")

        sf  = tkfont.Font(family="Segoe UI", size=11)
        hf  = tkfont.Font(family="Segoe UI", size=20, weight="bold")
        bf  = tkfont.Font(family="Segoe UI", size=13)
        mf  = tkfont.Font(family="Segoe UI", size=13)

        wrap = tk.Frame(r, bg=BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        self.title_lbl = tk.Label(wrap, text="", font=hf, bg=BG, fg=FG)
        self.title_lbl.pack(pady=(0, 4))

        self.sub_lbl = tk.Label(wrap, text="", font=sf, bg=BG, fg=DIM)
        self.sub_lbl.pack(pady=(0, 32))

        # Progress rows
        grid = tk.Frame(wrap, bg=BG)
        grid.pack()

        self._rows: dict[str, tuple] = {}
        for i, (key, label) in enumerate([
            ("anki",     "Anki cards"),
            ("seterra",  "Seterra"),
            ("duolingo", "Duolingo"),
        ]):
            tk.Label(grid, text=label, font=sf, bg=BG, fg=DIM,
                     width=12, anchor="e").grid(row=i, column=0, padx=(0, 14), pady=6)
            val = tk.Label(grid, text="—", font=bf, bg=BG, fg=FG, width=18, anchor="w")
            val.grid(row=i, column=1, pady=6)
            bar = tk.Canvas(grid, width=200, height=6,
                            bg="#220000", highlightthickness=0)
            bar.grid(row=i, column=2, padx=(12, 0), pady=6)
            self._rows[key] = (val, bar)

        # Message
        self.msg_lbl = tk.Label(wrap, text="", font=mf, bg=BG, fg=GOLD,
                                wraplength=520)
        self.msg_lbl.pack(pady=(36, 0))

        # Footer
        self.foot_lbl = tk.Label(wrap, text="", font=sf, bg=BG, fg=DIM)
        self.foot_lbl.pack(pady=(20, 0))

    def _set_bar(self, key: str, val: int, cap: int):
        lbl, canvas = self._rows[key]
        if cap > 0:
            pct = min(1.0, val / cap)
            done = pct >= 1.0
            color = GRN if done else GOLD
            text = (f"{val} / {cap}" if key == "anki"
                    else f"{fmt_time(val)} / {fmt_time(cap)}")
            canvas.delete("all")
            canvas.create_rectangle(0, 0, int(200 * pct), 6, fill=color, outline="")
        else:
            # No target — show raw value, no bar
            text = str(val) if key == "anki" else fmt_time(val)
            canvas.delete("all")
        lbl.config(text=text, fg=FG)

    def _update(self, st: dict):
        self.title_lbl.config(text=REASON_TEXT.get(st["reason"], "Locked"))
        self.sub_lbl.config(text=f"Gaming and entertainment are currently restricted.")

        self._set_bar("anki",     st["anki_cards"],    st["anki_target"])
        self._set_bar("seterra",  st["seterra_secs"],  st["seterra_target"])
        self._set_bar("duolingo", st["duolingo_secs"], st["duolingo_target"])

        msgs = st.get("messages", [])
        self.msg_lbl.config(text=msgs[-1]["text"] if msgs else "")

        ok = st["server_ok"]
        self.foot_lbl.config(
            text="● connected" if ok else "○ server unreachable",
            fg=GRN if ok else RED,
        )

    def _tick(self):
        st = snap()
        if st["locked"]:
            if not self._visible:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self._visible = True
            self._update(st)
        else:
            if self._visible:
                self.root.withdraw()
                self._visible = False
        self.root.after(2000, self._tick)

    def run(self):
        self.root.mainloop()

# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for fn, name in [(tick_loop, "tick"), (poll_loop, "poll")]:
        threading.Thread(target=fn, name=name, daemon=True).start()
    Overlay().run()
