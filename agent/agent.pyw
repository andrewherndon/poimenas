"""
Windows Diagnostic Core Service
System health monitoring and telemetry agent
"""
from __future__ import annotations
import sys, json, time, threading, logging, subprocess
from pathlib import Path
from datetime import date

import requests
import psutil
import win32api
import win32gui
import win32process
import tkinter as tk
from tkinter import font as tkfont, simpledialog, messagebox

# ── Config ────────────────────────────────────────────────────────────────────

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

with open(BASE / "config.json") as f:
    cfg = json.load(f)

SERVER         = cfg["server_url"].rstrip("/")
KEY            = cfg["api_key"]
BLOCKED_PROCS  = {p.lower() for p in cfg.get("blocked_processes", [])}
WEB_TARGETS    = cfg.get("web_targets", {
    "seterra":  ["seterra.com", "seterra"],
    "duolingo": ["duolingo.com", "duolingo"],
})
BYPASS_SECRET  = cfg.get("bypass_secret", 0)   # 0 = feature disabled
BYPASS_MINUTES = cfg.get("bypass_minutes", 60)
VERSION = "0.1.0"
IDLE_CUTOFF = 30   # seconds without input → stop counting web time
POLL_SECS   = 30
WARN_AT     = [3600, 1800, 900, 300]  # remaining seconds that trigger a warning

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
    rule_type="",    earn_rate=2.0,
    warning_text="", warning_until=0.0,
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
_warned: set = set()   # thresholds (seconds) already warned today

def _maybe_reset():
    global _day, _seterra, _duolingo, _gaming, _warned
    with _cnt_mu:
        today = date.today().isoformat()
        if today != _day:
            _day, _seterra, _duolingo, _gaming = today, 0, 0, 0
            _warned = set()

def _counts():
    with _cnt_mu:
        return _seterra, _duolingo, _gaming

def _check_warn(g_secs: int, g_cap: int):
    global _warned
    remaining = g_cap - g_secs
    with _cnt_mu:
        for threshold in WARN_AT:
            if remaining <= threshold and threshold not in _warned:
                _warned.add(threshold)
                mins = threshold // 60
                ss(
                    warning_text=f"{mins} min of screen time remaining",
                    warning_until=time.time() + 45,
                )
                break   # one warning per poll; next threshold fires next time

# ── Bypass ────────────────────────────────────────────────────────────────────

BYPASS_USED_FILE = BASE / "bypass_used.txt"

def _bypass_used_today() -> bool:
    try:
        return BYPASS_USED_FILE.read_text().strip() == date.today().isoformat()
    except Exception:
        return False

def _mark_bypass_used():
    try:
        BYPASS_USED_FILE.write_text(date.today().isoformat())
    except Exception:
        pass

def _daily_password() -> str:
    if not BYPASS_SECRET:
        return ""
    d = date.today()
    return str((d.month * d.day * BYPASS_SECRET) % 10000).zfill(4)

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
    try:
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=5)
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
            rule   = status.get("rule") or {}

            ss(
                locked=locked,
                reason=status.get("reason", ""),
                server_ok=True,
                messages=status.get("pending_messages", []),
                anki_cards=anki,    anki_target=t.get("anki_target", 0),
                seterra_secs=s,     seterra_target=t.get("seterra_target_seconds", 0),
                duolingo_secs=d,    duolingo_target=t.get("duolingo_target_seconds", 0),
                gaming_secs=g,      gaming_cap=t.get("gaming_cap_seconds"),
                rule_type=rule.get("type", ""),
                earn_rate=rule.get("earn_rate", 2.0),
            )

            if locked and not was_locked:
                suspend_blocked()
            elif not locked and was_locked:
                resume_blocked()
            was_locked = locked

            cap = t.get("gaming_cap_seconds")
            if not locked and cap:
                _check_warn(g, cap)

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

# ── Widget ────────────────────────────────────────────────────────────────────

BG   = "#0d0000"
FG   = "#FAF7F0"
GOLD = "#D4AF37"
RED  = "#ef4444"
GRN  = "#4ade80"
DIM  = "#666666"
ORG  = "#f97316"

REASON_TEXT = {
    "prerequisite": "Finish your daily tasks",
    "cap_exceeded":  "Screen time limit reached",
    "manual":        "Locked by Andrew",
    "":              "Locked",
}

WIDGET_W = 270
WIDGET_H = 275


def _widget_pos() -> tuple[int, int]:
    """Bottom-right corner of the non-primary monitor, or primary if only one."""
    try:
        primary = secondary = None
        for m in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(m[0])
            l, t, r, b = info['Monitor']
            if info['Flags'] & 1:
                primary = (l, t, r, b)
            else:
                secondary = (l, t, r, b)
        l, t, r, b = secondary if secondary else primary
        return r - WIDGET_W - 16, b - WIDGET_H - 48
    except Exception:
        return 1620, 820


class Widget:
    def __init__(self):
        self.root = tk.Tk()
        self._last_msg  = ""
        self._last_warn = ""
        self._build()
        self.root.after(2000, self._tick)

    def _build(self):
        r = self.root
        x, y = _widget_pos()
        r.title("Anti-Brainrot System")
        r.configure(bg=BG)
        r.geometry(f"{WIDGET_W}x{WIDGET_H}+{x}+{y}")
        r.attributes("-topmost", True)
        r.resizable(False, False)

        sf   = tkfont.Font(family="Segoe UI", size=9)
        bf   = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        btnf = tkfont.Font(family="Segoe UI", size=8)
        self._mf = tkfont.Font(family="Segoe UI", size=11, weight="bold")

        pad = tk.Frame(r, bg=BG, padx=12, pady=10)
        pad.pack(fill="both", expand=True)

        self.status_lbl = tk.Label(pad, text="", font=bf, bg=BG, fg=FG, anchor="w")
        self.status_lbl.pack(fill="x", pady=(0, 6))

        self._rows: dict[str, tk.Label] = {}
        for key, label in [("anki", "Anki"), ("seterra", "Seterra"), ("duolingo", "Duolingo")]:
            row = tk.Frame(pad, bg=BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{label}:", font=sf, bg=BG, fg=DIM,
                     width=9, anchor="w").pack(side="left")
            val = tk.Label(row, text="—", font=sf, bg=BG, fg=FG, anchor="w")
            val.pack(side="left")
            self._rows[key] = val

        self.gaming_lbl = tk.Label(pad, text="", font=sf, bg=BG, fg=DIM, anchor="w")
        self.gaming_lbl.pack(fill="x", pady=(4, 0))

        self.warn_lbl = tk.Label(pad, text="", font=sf, bg=BG, fg=ORG, anchor="w")
        self.warn_lbl.pack(fill="x", pady=(2, 0))

        # Buttons (earn info + optional bypass)
        btn_row = tk.Frame(pad, bg=BG)
        btn_row.pack(fill="x", pady=(4, 0))

        self.earn_btn = tk.Button(
            btn_row, text="How to Unlock", font=btnf,
            bg="#1a1a1a", fg=GOLD, relief="flat", bd=1,
            command=self._show_earn_info,
        )
        self.earn_btn.pack(side="left", padx=(0, 4))

        if BYPASS_SECRET:
            self.bypass_btn: tk.Button | None = tk.Button(
                btn_row, text="Daily Bypass", font=btnf,
                bg="#1a1a1a", fg=FG, relief="flat", bd=1,
                command=self._do_bypass,
            )
            self.bypass_btn.pack(side="left")
        else:
            self.bypass_btn = None

        self.msg_lbl = tk.Label(pad, text="", font=self._mf, bg=BG, fg=GOLD,
                                wraplength=240, anchor="w", justify="left")
        self.msg_lbl.pack(fill="x", pady=(6, 0))

        self.foot_lbl = tk.Label(pad, text="", font=sf, bg=BG, fg=DIM, anchor="w")
        self.foot_lbl.pack(fill="x", pady=(4, 0))

    # ── Button actions ────────────────────────────────────────────────────────

    def _show_earn_info(self):
        st = snap()
        rule_type = st.get("rule_type", "")
        earn_rate = st.get("earn_rate", 2.0)

        if rule_type == "earn_more":
            msg = (
                f"Earn more gaming time by studying:\n\n"
                f"• 1 Anki card  =  1 min gaming\n"
                f"• 1 min Seterra  =  {earn_rate:.0f} min gaming\n"
                f"• 1 min Duolingo  =  {earn_rate:.0f} min gaming\n\n"
                f"Keep studying and gaming will unlock automatically."
            )
        elif rule_type == "prerequisite":
            tasks = []
            if st["anki_target"] > 0 and st["anki_cards"] < st["anki_target"]:
                tasks.append(f"Anki: {st['anki_cards']} / {st['anki_target']} cards")
            if st["seterra_target"] > 0 and st["seterra_secs"] < st["seterra_target"]:
                tasks.append(f"Seterra: {fmt_time(st['seterra_secs'])} / {fmt_time(st['seterra_target'])}")
            if st["duolingo_target"] > 0 and st["duolingo_secs"] < st["duolingo_target"]:
                tasks.append(f"Duolingo: {fmt_time(st['duolingo_secs'])} / {fmt_time(st['duolingo_target'])}")
            msg = (
                ("Complete these tasks to unlock:\n\n" + "\n".join(f"• {t}" for t in tasks))
                if tasks else "Tasks complete — waiting for server sync."
            )
        else:
            msg = "Gaming is locked.\n\nContact Andrew to unlock."

        messagebox.showinfo("How to Unlock", msg, parent=self.root)

    def _do_bypass(self):
        if _bypass_used_today():
            messagebox.showinfo("Daily Bypass", "Daily bypass already used today.", parent=self.root)
            return
        code = simpledialog.askstring("Daily Bypass", "Enter today's code:", show="*", parent=self.root)
        if code is None:
            return
        if code.strip() == _daily_password():
            try:
                requests.post(
                    f"{SERVER}/api/lock", headers=HEADERS, timeout=8,
                    json={"locked": False, "reason": "daily_bypass",
                          "duration_minutes": BYPASS_MINUTES},
                )
                _mark_bypass_used()
                messagebox.showinfo(
                    "Daily Bypass",
                    f"Unlocked for {BYPASS_MINUTES} minutes.",
                    parent=self.root,
                )
            except Exception as e:
                log.warning("bypass request failed: %s", e)
                messagebox.showerror("Daily Bypass", "Could not reach server.", parent=self.root)
        # wrong code: silent fail

    # ── Blink helpers ─────────────────────────────────────────────────────────

    def _blink(self, count: int = 6):
        self.msg_lbl.config(fg=BG if count % 2 == 0 else GOLD)
        if count > 0:
            self.root.after(300, lambda: self._blink(count - 1))

    def _blink_warn(self, count: int = 6):
        self.warn_lbl.config(fg=BG if count % 2 == 0 else ORG)
        if count > 0:
            self.root.after(300, lambda: self._blink_warn(count - 1))

    # ── Row text ──────────────────────────────────────────────────────────────

    def _row_text(self, val: int, target: int, is_anki: bool = False) -> str:
        if target > 0:
            done = "[+] " if val >= target else ""
            return f"{done}{val} / {target}" if is_anki else f"{done}{fmt_time(val)} / {fmt_time(target)}"
        return str(val) if is_anki else fmt_time(val)

    # ── Tick ──────────────────────────────────────────────────────────────────

    def _update(self, st: dict):
        locked = st["locked"]
        self.status_lbl.config(
            text=f"LOCKED  {REASON_TEXT.get(st['reason'], '')}" if locked else "Unlocked",
            fg=RED if locked else GRN,
        )

        self._rows["anki"].config(text=self._row_text(st["anki_cards"], st["anki_target"], True))
        self._rows["seterra"].config(text=self._row_text(st["seterra_secs"], st["seterra_target"]))
        self._rows["duolingo"].config(text=self._row_text(st["duolingo_secs"], st["duolingo_target"]))

        cap = st.get("gaming_cap")
        g   = st["gaming_secs"]
        if cap:
            self.gaming_lbl.config(
                text=f"Gaming: {fmt_time(g)} / {fmt_time(cap)}  ({fmt_time(max(0, cap - g))} left)"
            )
        else:
            self.gaming_lbl.config(text=f"Gaming: {fmt_time(g)}")

        # Time warning
        w_text  = st.get("warning_text", "")
        w_until = st.get("warning_until", 0.0)
        if w_text and time.time() < w_until:
            if w_text != self._last_warn:
                self._last_warn = w_text
                self.warn_lbl.config(text=w_text, fg=ORG)
                self._blink_warn(6)
            # else: already showing, leave it
        else:
            self.warn_lbl.config(text="")
            if not w_text:
                self._last_warn = ""

        # Buttons
        self.earn_btn.config(state="normal" if locked else "disabled")
        if self.bypass_btn:
            used = _bypass_used_today()
            self.bypass_btn.config(
                state="disabled" if (not locked or used) else "normal",
                text="Bypass Used" if used else "Daily Bypass",
            )

        # Messages from Andrew
        msgs    = st.get("messages", [])
        new_msg = msgs[-1]["text"] if msgs else ""
        if new_msg != self._last_msg:
            self._last_msg = new_msg
            self.msg_lbl.config(text=new_msg)
            if new_msg:
                self._blink(6)
        elif not new_msg:
            self.msg_lbl.config(text="")

        ok = st["server_ok"]
        self.foot_lbl.config(text="● connected" if ok else "○ server unreachable",
                             fg=GRN if ok else RED)

    def _tick(self):
        self._update(snap())
        self.root.after(2000, self._tick)

    def run(self):
        self.root.mainloop()

# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for fn, name in [(tick_loop, "tick"), (poll_loop, "poll")]:
        threading.Thread(target=fn, name=name, daemon=True).start()
    Widget().run()
