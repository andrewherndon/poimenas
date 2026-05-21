# Project: Poimenas

## Context

A lightweight parental control system for a Windows PC (belonging to Andrew's younger brother). The goal is to require completion of a mentally stimulating task before access to games, YouTube, and other entertainment is granted. The system relies on obfuscation and hardening rather than permission boundaries — the controlled user has an admin account (needed to install games).

A Raspberry Pi 4 (4GB) in Andrew's room is the control plane. The dashboard lives at `andrew.by/watch` — a password-protected page on Andrew's Next.js personal site (Vercel), which proxies to the RPi's FastAPI via Tailscale Funnel.

## Goals

- Gate entertainment apps behind configurable daily learning requirements (Anki, Seterra, Duolingo)
- Allowlist-based web policy: only permitted domains resolve when locked
- Track usage and sync logs off-device
- Remote lock/unlock, messaging, and rule changes from any device
- Resistant to casual circumvention by a non-technical user with ChatGPT access
- Fine-grained, flexible rule control from the dashboard

## Learning Task Sources

**Anki** — AnkiConnect REST API on localhost:8765. Completion = N cards reviewed today.

**Web-based sites (Seterra, Duolingo)** — Completion = accumulated active time. Agent monitors foreground browser window title; 30s idle timeout pauses the timer. Targets configurable in `config.json`.

## Rule System

- **Prerequisite**: must finish learning tasks before anything unlocks
- **Cap**: no prerequisite, gated apps limited to a daily time cap
- **Earn-more**: base cap + extra time earned per minute of learning
- **Free day**: no restrictions. Set per-day or by weekday from the dashboard.

## Architecture

```
┌──────────────────────────────────────────────────┐
│   andrew.by (Vercel — Next.js 15)                │
│   /watch          → password-gated dashboard     │
│   /api/poimenas/* → server-side proxy (API key)  │
└────────────────────┬─────────────────────────────┘
                     │ Tailscale Funnel (HTTPS)
┌────────────────────▼─────────────────────────────┐
│   RASPBERRY PI 4 — Andrew's room                 │
│                                                  │
│   FastAPI + SQLite                               │
│   ├── /api/status      → lock state + progress  │
│   ├── /api/lock        → manual override        │
│   ├── /api/message     → push overlay message   │
│   ├── /api/heartbeat   ← Windows agent          │
│   ├── /api/rules       → rule CRUD              │
│   ├── /api/dns/allowlist → domain CRUD          │
│   ├── /api/health      → RPi/dnsmasq status     │
│   ├── /api/logs        → event log              │
│   └── /api/update      → version manifest       │
│                                                  │
│   dnsmasq — allowlist DNS, rewritten on lock     │
│   Tailscale — local LAN + Funnel for Vercel      │
└────────────────────┬─────────────────────────────┘
                     │ HTTP over local LAN
┌────────────────────▼─────────────────────────────┐
│   WINDOWS MACHINE (brother's PC)                 │
│                                                  │
│   agent.pyw (scheduled task, runs at logon)      │
│   ├── 1s tick: active web time, gaming time      │
│   ├── 30s poll: /status, /heartbeat              │
│   ├── Suspends/resumes blocked PIDs              │
│   └── tkinter fullscreen overlay when locked     │
│                                                  │
│   watchdog.py (Windows Service — SYSTEM)         │
│   ├── Relaunches agent if killed (every 10s)     │
│   ├── Resets DNS to RPi if changed (every 60s)  │
│   └── Restores DoH registry policy (every 60s)  │
└──────────────────────────────────────────────────┘
```

## Web Policy (dnsmasq allowlist)

- RPi runs `dnsmasq`. Windows DNS is statically set to the RPi's local IP.
- **Locked:** dnsmasq uses `no-resolv` + per-domain `server=` entries. Only allowlisted domains (e.g. `seterra.com`, `duolingo.com`) resolve. Everything else returns SERVFAIL.
- **Unlocked:** dnsmasq forwards all queries to the router. Normal browsing, no latency impact.
- Config is rewritten and dnsmasq restarted automatically on every lock/unlock via `apply_dns.sh` (called with sudo via a sudoers rule).
- Allowlist is managed from the dashboard and stored in SQLite (`dns_allowlist` table).
- DoH disabled in Chrome/Edge via registry policy. Watchdog restores these if tampered.
- **Future:** graceful failsafe behavior when RPi or internet is down.

## Key Technical Decisions

**Obfuscation over permission hardening.** Agent lives at `C:\ProgramData\Microsoft\Windows\DiagnosticCore\`, scheduled task named "Windows Diagnostic Core Service", watchdog service named "Windows Telemetry Sync Host". Safe Mode disabled via `bcdedit` in the installer.

**Watchdog Windows Service.** Runs as SYSTEM. Monitors agent process every 10s, relaunches via `CreateProcessAsUser` in the active user session. Also checks/restores DNS and DoH policy every 60s.

**RPi as source of truth.** Lock state, rules, and allowlist are authoritative on the RPi. The Windows agent is a dumb enforcer. Logs synced off-device.

**Tailscale for networking.** No port forwarding. Tailscale Funnel exposes the RPi to Vercel. Windows agent uses the RPi's LAN IP directly.

## Deployment Status

| Component | Status |
|---|---|
| RPi FastAPI server | Deployed, running as systemd service |
| dnsmasq | Installed and configured on RPi |
| Tailscale Funnel | Active |
| Dashboard (andrew.by/watch) | Built, on `poimenas-watch` branch, not yet merged to prod |
| Windows agent | Built, not yet deployed to brother's PC |
| Windows watchdog | Built, not yet deployed |

## Stack

| Component | Tech |
|---|---|
| RPi server | Python, FastAPI, SQLite |
| DNS enforcement | dnsmasq (RPi) |
| Dashboard | Next.js 15 + Tailwind, Vercel (`poimenas-watch` branch) |
| Dashboard auth | Single password → HMAC-signed cookie |
| RPi exposure | Tailscale Funnel |
| Windows agent | Python, pywin32, psutil, tkinter |
| Windows watchdog | Python, pywin32 (Windows Service) |
| Packaging | PyInstaller → single exe (planned) |
| Networking | Tailscale |
| Anki | AnkiConnect REST API |
| Web time tracking | pywin32 foreground window + GetLastInputInfo |
