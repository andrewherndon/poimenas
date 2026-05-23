from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Literal
import os, time, subprocess
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv
from db import get_db, init_db

load_dotenv()

app = FastAPI(title="Poimenas")
API_KEY   = os.environ["POIMENAS_API_KEY"]
ROUTER_IP = os.environ.get("ROUTER_IP", "192.168.1.1")
VERSION   = "0.1.0"

HERE = Path(__file__).parent
DNS_STAGING  = HERE / "dnsmasq_current.conf"
APPLY_SCRIPT = HERE / "apply_dns.sh"


# ── DNS ───────────────────────────────────────────────────────────────────────

def _write_dns(locked: bool, domains: list[str]):
    if locked:
        lines = ["# poimenas - locked (allowlist)", "no-resolv", "address=/#/0.0.0.0", "cache-size=0", ""]
        for d in domains:
            lines.append(f"server=/{d}/8.8.8.8")
        conf = "\n".join(lines) + "\n"
    else:
        conf = f"# poimenas - unlocked\nserver={ROUTER_IP}\ncache-size=1000\n"
    DNS_STAGING.write_text(conf)
    try:
        subprocess.run(["sudo", str(APPLY_SCRIPT)], capture_output=True, timeout=10)
    except Exception:
        pass  # dnsmasq may not be installed during dev


def apply_dns(locked: bool, db):
    domains = [
        r["domain"] for r in
        db.execute("SELECT domain FROM dns_allowlist ORDER BY domain").fetchall()
    ]
    _write_dns(locked, domains)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()
    with get_db() as db:
        override = dict(db.execute("SELECT * FROM lock_overrides WHERE id=1").fetchone())
        apply_dns(bool(override["locked"]), db)


def auth(request: Request):
    key = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def today() -> str:
    return date.today().isoformat()


def get_active_rule(db):
    today_str = today()
    weekday = datetime.today().strftime("%a").lower()
    hour = datetime.today().hour
    row = db.execute(
        """SELECT * FROM rules
           WHERE day IN (?, ?, 'default')
           AND (start_hour IS NULL OR (? >= start_hour AND ? < end_hour))
           ORDER BY CASE day WHEN ? THEN 2 WHEN ? THEN 1 ELSE 0 END DESC,
                    priority DESC
           LIMIT 1""",
        (today_str, weekday, hour, hour, today_str, weekday),
    ).fetchone()
    return dict(row) if row else None


def compute_lock(rule, stats, override) -> tuple[bool, str]:
    if override["locked"]:
        until = override["until_ts"]
        if until == 0 or time.time() < until:
            return True, override["reason"] or "manual"

    # Timed unlock (daily bypass or approved extension)
    if not override["locked"] and override["until_ts"] > 0 and time.time() < override["until_ts"]:
        return False, override["reason"] or "bypass"

    if not rule:
        return False, ""

    t = rule["type"]

    if t == "free":
        return False, ""

    elif t == "prerequisite":
        anki_ok = rule["anki_target"] == 0 or stats["anki_cards"] >= rule["anki_target"]
        seterra_ok = rule["seterra_target_seconds"] == 0 or stats["seterra_active_seconds"] >= rule["seterra_target_seconds"]
        duolingo_ok = rule["duolingo_target_seconds"] == 0 or stats["duolingo_active_seconds"] >= rule["duolingo_target_seconds"]
        if not (anki_ok and seterra_ok and duolingo_ok):
            return True, "prerequisite"
        return False, ""

    elif t == "cap":
        if rule["gaming_cap_seconds"] and stats["gaming_seconds"] >= rule["gaming_cap_seconds"]:
            return True, "cap_exceeded"
        return False, ""

    elif t == "earn_more":
        earned = (
            stats["anki_cards"] * 60
            + stats["seterra_active_seconds"]
            + stats["duolingo_active_seconds"]
        ) * rule["earn_rate"]
        if stats["gaming_seconds"] >= rule["gaming_cap_seconds"] + earned:
            return True, "cap_exceeded"
        return False, ""

    return False, ""


def empty_stats() -> dict:
    return {
        "anki_cards": 0,
        "seterra_active_seconds": 0,
        "duolingo_active_seconds": 0,
        "gaming_seconds": 0,
        "last_heartbeat_ts": None,
        "agent_version": None,
    }


# ── Status ────────────────────────────────────────────────────────────────────

@app.get("/api/status", dependencies=[Depends(auth)])
def get_status():
    with get_db() as db:
        row = db.execute("SELECT * FROM daily_stats WHERE date=?", (today(),)).fetchone()
        stats = dict(row) if row else empty_stats()

        override = dict(db.execute("SELECT * FROM lock_overrides WHERE id=1").fetchone())
        rule = get_active_rule(db)
        locked, reason = compute_lock(rule, stats, override)

        last_hb = stats.get("last_heartbeat_ts")
        agent_online = bool(last_hb and time.time() - last_hb < 90)

        msgs = [
            dict(r)
            for r in db.execute(
                "SELECT id, text FROM pending_messages WHERE delivered=0 ORDER BY ts"
            ).fetchall()
        ]

        ext_row = db.execute(
            "SELECT * FROM extension_requests WHERE status='pending' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        pending_ext = dict(ext_row) if ext_row else None

    return {
        "locked": locked,
        "reason": reason,
        "agent_online": agent_online,
        "last_heartbeat": datetime.fromtimestamp(last_hb).isoformat() if last_hb else None,
        "today": {
            "anki_cards": stats["anki_cards"],
            "anki_target": rule["anki_target"] if rule else 0,
            "seterra_active_seconds": stats["seterra_active_seconds"],
            "seterra_target_seconds": rule["seterra_target_seconds"] if rule else 0,
            "duolingo_active_seconds": stats["duolingo_active_seconds"],
            "duolingo_target_seconds": rule["duolingo_target_seconds"] if rule else 0,
            "gaming_seconds": stats["gaming_seconds"],
            "gaming_cap_seconds": rule["gaming_cap_seconds"] if rule else None,
        },
        "rule": rule,
        "pending_messages": msgs,
        "pending_extension": pending_ext,
    }


# ── Lock ──────────────────────────────────────────────────────────────────────

class LockRequest(BaseModel):
    locked: bool
    reason: str = ""
    duration_minutes: Optional[int] = None


@app.post("/api/lock", dependencies=[Depends(auth)])
def set_lock(body: LockRequest):
    until_ts = 0.0
    if body.duration_minutes:
        until_ts = time.time() + body.duration_minutes * 60
    with get_db() as db:
        db.execute(
            "UPDATE lock_overrides SET locked=?, reason=?, until_ts=? WHERE id=1",
            (int(body.locked), body.reason, until_ts),
        )
        db.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
            (time.time(), "lock" if body.locked else "unlock", body.reason),
        )
        db.commit()
        apply_dns(body.locked, db)
    return {"ok": True}


# ── Message ───────────────────────────────────────────────────────────────────

class MessageRequest(BaseModel):
    text: str


@app.post("/api/message", dependencies=[Depends(auth)])
def send_message(body: MessageRequest):
    with get_db() as db:
        db.execute(
            "INSERT INTO pending_messages (ts, text) VALUES (?,?)",
            (time.time(), body.text),
        )
        db.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
            (time.time(), "message", body.text),
        )
        db.commit()
    return {"ok": True}


# ── Heartbeat ─────────────────────────────────────────────────────────────────

class HeartbeatRequest(BaseModel):
    agent_version: str = "0.0.0"
    anki_cards: int = 0
    seterra_active_seconds: int = 0
    duolingo_active_seconds: int = 0
    gaming_seconds: int = 0
    process_times: dict[str, int] = {}


@app.post("/api/heartbeat", dependencies=[Depends(auth)])
def heartbeat(body: HeartbeatRequest):
    with get_db() as db:
        db.execute(
            """INSERT INTO daily_stats
                   (date, anki_cards, seterra_active_seconds, duolingo_active_seconds,
                    gaming_seconds, last_heartbeat_ts, agent_version)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                   anki_cards=excluded.anki_cards,
                   seterra_active_seconds=excluded.seterra_active_seconds,
                   duolingo_active_seconds=excluded.duolingo_active_seconds,
                   gaming_seconds=excluded.gaming_seconds,
                   last_heartbeat_ts=excluded.last_heartbeat_ts,
                   agent_version=excluded.agent_version""",
            (
                today(), body.anki_cards, body.seterra_active_seconds,
                body.duolingo_active_seconds, body.gaming_seconds,
                time.time(), body.agent_version,
            ),
        )
        for proc, secs in body.process_times.items():
            db.execute(
                """INSERT INTO process_stats (date, process, seconds) VALUES (?,?,?)
                   ON CONFLICT(date, process) DO UPDATE SET seconds=excluded.seconds""",
                (today(), proc[:64], secs),
            )
        db.execute("UPDATE pending_messages SET delivered=1 WHERE delivered=0")
        db.commit()
    return {"ok": True}


# ── Rules ─────────────────────────────────────────────────────────────────────

class RuleBody(BaseModel):
    day: str
    type: Literal["prerequisite", "cap", "earn_more", "free"]
    anki_target: int = 0
    seterra_target_seconds: int = 0
    duolingo_target_seconds: int = 0
    gaming_cap_seconds: int = 7200
    earn_rate: float = 2.0
    priority: int = 0
    start_hour: Optional[int] = None
    end_hour: Optional[int] = None


@app.get("/api/rules", dependencies=[Depends(auth)])
def list_rules():
    with get_db() as db:
        rows = db.execute("SELECT * FROM rules ORDER BY priority DESC, day").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/rules", dependencies=[Depends(auth)])
def create_rule(body: RuleBody):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO rules
                   (day, type, anki_target, seterra_target_seconds, duolingo_target_seconds,
                    gaming_cap_seconds, earn_rate, priority, start_hour, end_hour)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (body.day, body.type, body.anki_target, body.seterra_target_seconds,
             body.duolingo_target_seconds, body.gaming_cap_seconds, body.earn_rate,
             body.priority, body.start_hour, body.end_hour),
        )
        db.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
            (time.time(), "rule_change", f"created {cur.lastrowid} ({body.day}/{body.type})"),
        )
        db.commit()
    return {"id": cur.lastrowid}


@app.put("/api/rules/{rule_id}", dependencies=[Depends(auth)])
def update_rule(rule_id: int, body: RuleBody):
    with get_db() as db:
        db.execute(
            """UPDATE rules SET day=?, type=?, anki_target=?, seterra_target_seconds=?,
               duolingo_target_seconds=?, gaming_cap_seconds=?, earn_rate=?, priority=?,
               start_hour=?, end_hour=?
               WHERE id=?""",
            (body.day, body.type, body.anki_target, body.seterra_target_seconds,
             body.duolingo_target_seconds, body.gaming_cap_seconds,
             body.earn_rate, body.priority, body.start_hour, body.end_hour, rule_id),
        )
        db.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
            (time.time(), "rule_change", f"updated {rule_id}"),
        )
        db.commit()
    return {"ok": True}


@app.delete("/api/rules/{rule_id}", dependencies=[Depends(auth)])
def delete_rule(rule_id: int):
    with get_db() as db:
        db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        db.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
            (time.time(), "rule_change", f"deleted {rule_id}"),
        )
        db.commit()
    return {"ok": True}


# ── Extension requests ────────────────────────────────────────────────────────

class ExtensionBody(BaseModel):
    reason: str = ""
    duration_minutes: int = 30


@app.post("/api/extension", dependencies=[Depends(auth)])
def request_extension(body: ExtensionBody):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO extension_requests (ts, reason, duration_minutes) VALUES (?,?,?)",
            (time.time(), body.reason, body.duration_minutes),
        )
        db.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
            (time.time(), "extension_request",
             f"requested {body.duration_minutes}m: {body.reason}"),
        )
        db.commit()
    return {"id": cur.lastrowid}


@app.get("/api/extension/pending", dependencies=[Depends(auth)])
def get_pending_extensions():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM extension_requests WHERE status='pending' ORDER BY ts DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/extension/{ext_id}/approve", dependencies=[Depends(auth)])
def approve_extension(ext_id: int):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM extension_requests WHERE id=?", (ext_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        row = dict(row)
        until_ts = time.time() + row["duration_minutes"] * 60
        db.execute(
            "UPDATE extension_requests SET status='approved', resolved_ts=? WHERE id=?",
            (time.time(), ext_id),
        )
        db.execute(
            "UPDATE lock_overrides SET locked=0, reason='extension', until_ts=? WHERE id=1",
            (until_ts,),
        )
        db.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
            (time.time(), "extension_approved",
             f"approved {row['duration_minutes']}m for: {row['reason']}"),
        )
        db.commit()
        apply_dns(False, db)
    return {"ok": True}


@app.post("/api/extension/{ext_id}/deny", dependencies=[Depends(auth)])
def deny_extension(ext_id: int):
    with get_db() as db:
        db.execute(
            "UPDATE extension_requests SET status='denied', resolved_ts=? WHERE id=?",
            (time.time(), ext_id),
        )
        db.execute(
            "INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
            (time.time(), "extension_denied", f"denied {ext_id}"),
        )
        db.commit()
    return {"ok": True}


# ── DNS Allowlist ─────────────────────────────────────────────────────────────

class DomainBody(BaseModel):
    domain: str


@app.get("/api/dns/allowlist", dependencies=[Depends(auth)])
def list_allowlist():
    with get_db() as db:
        rows = db.execute("SELECT * FROM dns_allowlist ORDER BY domain").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/dns/allowlist", dependencies=[Depends(auth)])
def add_domain(body: DomainBody):
    domain = body.domain.lower().strip().lstrip("www.").lstrip("https://").lstrip("http://").split("/")[0]
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO dns_allowlist (domain) VALUES (?)", (domain,))
        db.execute("INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
                   (time.time(), "dns_allowlist", f"added {domain}"))
        db.commit()
        override = dict(db.execute("SELECT * FROM lock_overrides WHERE id=1").fetchone())
        apply_dns(bool(override["locked"]), db)
    return {"ok": True}


@app.delete("/api/dns/allowlist/{domain_id}", dependencies=[Depends(auth)])
def remove_domain(domain_id: int):
    with get_db() as db:
        row = db.execute("SELECT domain FROM dns_allowlist WHERE id=?", (domain_id,)).fetchone()
        if row:
            db.execute("DELETE FROM dns_allowlist WHERE id=?", (domain_id,))
            db.execute("INSERT INTO events (ts, type, detail) VALUES (?,?,?)",
                       (time.time(), "dns_allowlist", f"removed {row['domain']}"))
            db.commit()
            override = dict(db.execute("SELECT * FROM lock_overrides WHERE id=1").fetchone())
            apply_dns(bool(override["locked"]), db)
    return {"ok": True}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats/processes", dependencies=[Depends(auth)])
def get_process_stats(date: str = None):
    d = date or today()
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM process_stats WHERE date=? ORDER BY seconds DESC", (d,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health", dependencies=[Depends(auth)])
def get_health():
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "dnsmasq"],
            capture_output=True, text=True, timeout=5,
        )
        dnsmasq_running = result.stdout.strip() == "active"
    except Exception:
        dnsmasq_running = False

    try:
        with get_db() as db:
            override = dict(db.execute("SELECT locked FROM lock_overrides WHERE id=1").fetchone())
            allowlist = [
                dict(r) for r in
                db.execute("SELECT id, domain FROM dns_allowlist ORDER BY domain").fetchall()
            ]
        db_ok = True
        dns_locked = bool(override["locked"])
    except Exception:
        db_ok = False
        dns_locked = False
        allowlist = []

    return {
        "version": VERSION,
        "db_ok": db_ok,
        "dnsmasq_running": dnsmasq_running,
        "dns_locked": dns_locked,
        "allowlist": allowlist,
    }


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/api/logs", dependencies=[Depends(auth)])
def get_logs(limit: int = 50):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Update manifest (no auth — polled by Windows agent) ──────────────────────

@app.get("/api/update")
def get_update():
    return {"version": VERSION, "url": None}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
