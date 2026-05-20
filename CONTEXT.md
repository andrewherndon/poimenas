# Project: Heimdall

## Context

A lightweight parental control system for a Windows PC (belonging to Andrew's younger brother). The goal is to require completion of a mentally stimulating task before access to games, YouTube, and other entertainment is granted. The system prioritizes being difficult to circumvent by a non-technical user while preserving a normal user experience once tasks are complete. The controlled user retains an admin account, so the system relies on obfuscation and hardening rather than permission boundaries.

A Raspberry Pi 4 (4GB) in Andrew's room acts as the off-device control plane, enabling remote locking, messaging, activity monitoring, and rule configuration from outside the home network. The dashboard is accessible at `andrew.by/watch` — a password-protected page on Andrew's existing Next.js personal site (hosted on Vercel), which proxies to the RPi's FastAPI via Tailscale Funnel.

## Goals

- Gate entertainment apps behind configurable daily learning requirements (Anki, Seterra, Duolingo, etc.)
- Track per-app usage time and sync logs off-device
- Allow remote lock/unlock, messaging, and rule changes from any device via Tailscale
- Be resistant to casual circumvention by a non-technical user with ChatGPT access
- Preserve normal admin-level PC usage once daily task is complete
- Give the parent (Andrew) fine-grained, flexible control over rules from the dashboard

## Learning Task Sources

The system supports multiple learning signals, not just Anki:

**Anki (local app)** — Polls AnkiConnect REST API on localhost:8765. Completion = N cards reviewed today. Precise and offline-capable.

**Web-based sites (Seterra, Duolingo, etc.)** — No public API. Completion = accumulated *active* time on the site. The Windows service monitors the foreground browser window URL and tracks time only while the user is active (mouse or keyboard event within the last 30 seconds). Idle sessions do not count. This prevents the "leave it open and walk away" bypass.

Supported web targets initially: `seterra.com` (geography/map memorization), `duolingo.com`. Easy to add more via config.

## Rule System (Parent-Controlled)

Andrew controls rules remotely from the dashboard. Rules are stored on the Linux server and fetched by the Windows service. Supported rule types:

- **Prerequisite mode**: Must complete X minutes on Seterra (or N Anki cards) before any gated apps unlock. No time limit after that.
- **Cap mode**: No prerequisite, but gated apps are limited to a daily cap (e.g., 2 hours total).
- **Earn-more mode**: Starts with a base cap; additional learning time unlocks additional screen time (e.g., 30 min Seterra = +1 hour gaming).
- **Free day**: No prerequisite, no cap. Can be set per-day from the dashboard.
- Rules can be scheduled per day-of-week or set as one-offs from the dashboard.

## Architecture

```
┌──────────────────────────────────────────────────┐
│   andrew.by (Vercel — Next.js personal site)     │
│                                                  │
│   /watch          → password-gated dashboard UI  │
│   /api/heimdall/* → proxy routes (server-side)   │
│        │  HTTPS + API key (env var)              │
└────────┼─────────────────────────────────────────┘
         │ Tailscale Funnel (public HTTPS endpoint)
┌────────▼─────────────────────────────────────────┐
│   RASPBERRY PI 4 (4GB) — Andrew's room           │
│                                                  │
│   FastAPI                                        │
│   ├── GET  /status       → lock state + rule     │
│   ├── POST /lock         → override lock/unlock  │
│   ├── POST /message      → push to overlay       │
│   ├── POST /heartbeat    ← from Windows          │
│   ├── GET/POST /rules    → rule CRUD             │
│   └── GET  /update       → version manifest      │
│                                                  │
│   SQLite — sessions, events, messages, rules     │
│   Tailscale — local network + Funnel for Vercel  │
└──────────────┬───────────────────────────────────┘
               │ HTTP over Tailscale (local network)
┌──────────────▼───────────────────────────────────┐
│   WINDOWS MACHINE (brother's PC)                 │
│                                                  │
│   Windows Service (obfuscated)                   │
│   ├── Polls /status every 30s                    │
│   ├── Polls AnkiConnect for card count           │
│   ├── Monitors foreground window + URL           │
│   ├── Tracks active web time (30s idle timeout)  │
│   ├── Suspends blocked PIDs via psutil           │
│   ├── Tracks app session time → SQLite           │
│   ├── POSTs heartbeat + usage data               │
│   └── Polls /update for self-updates             │
│                                                  │
│   Gatekeeper UI                                  │
│   └── Fullscreen topmost overlay,                │
│       dismissed only on task completion          │
└──────────────────────────────────────────────────┘
```

## Web Policy (Allowlist via RPi DNS)

Web blocking uses an **allowlist** approach — when locked, only explicitly permitted domains resolve. Everything else fails silently in all browsers.

**How it works:**
- The RPi runs `dnsmasq`, a lightweight DNS forwarder
- The Windows machine's DNS is statically pointed to the RPi's local IP
- A Windows Firewall rule (set by the installer) blocks outbound DNS (port 53) to any IP other than the RPi, preventing bypass by changing DNS settings
- Chrome/Edge DoH (DNS-over-HTTPS) is disabled via registry policy

**When locked:** dnsmasq resolves only the allowlisted domains (e.g. `seterra.com`, `duolingo.com`), returning NXDOMAIN for everything else. Works across all browsers and apps since it's OS-level.

**When unlocked:** dnsmasq forwards all queries to the router's DNS. Normal browsing, zero added latency. dnsmasq caches responses, so repeat lookups are faster than before.

**Performance:** DNS queries only happen at connection start. Actual traffic (games, video, web content) never touches the RPi. Local LAN DNS hop adds <1ms. No impact on gaming ping or connection quality.

**Future:** Handling RPi/internet downtime gracefully (failsafe DNS behavior) is noted for a future iteration.

## Key Technical Decisions

**Obfuscation over permission hardening.** Service is named and described to resemble a legitimate Windows system service. Executable lives in a system-looking path. Task is hidden in Task Scheduler under an innocuous name. Safe Mode disabled via bcdedit in the installer.

**Watchdog service.** A separate Windows Service (named to look like a system component) monitors the agent process and relaunches it if killed. This is the primary defense against Task Manager kills.

**AnkiConnect as completion signal for Anki.** Anki runs locally on Windows and exposes a REST API on localhost:8765. The service polls this for daily review count against a configured threshold. Offline-capable, no scraping required.

**Active-time tracking for web apps.** The agent monitors the foreground browser window title. A 30-second idle timeout (no mouse/keyboard input) pauses the timer, preventing the "leave it open and walk away" bypass.

**Linux server as source of truth.** Lock state and rules are authoritative on the RPi, not the Windows machine. The Windows agent is a dumb enforcer. Logs are synced off-device.

**Tailscale for networking.** No port forwarding, no public exposure. Both machines join a Tailscale network. Off-network access works transparently.

## Deployment Notes

- **Windows machine**: Runs on the brother's physical PC. Not a VPS — the deep system hooks (service ACLs, registry edits, process suspension, foreground window monitoring) require a real Windows environment.
- **RPi**: Runs FastAPI + SQLite. Tailscale is installed; `tailscale funnel` exposes the FastAPI port publicly so Vercel can reach it. The RPi is on Andrew's home LAN and communicates with the Windows PC over Tailscale.
- **Dashboard**: Served at `andrew.by/watch` via Vercel. Auth is a single password stored as a Vercel env var (`WATCH_PASSWORD`). On success, a signed cookie grants session access. Next.js API routes proxy to the RPi using `HEIMDALL_API_URL` (the Funnel URL) and `HEIMDALL_API_KEY` (shared secret), both Vercel env vars.
- **Remote updates**: Andrew SSHes into the RPi, deploys updated server code, and optionally drops a new Windows agent binary at the update endpoint. The Windows service picks it up on next poll.

## Stack

| Component | Tech |
|---|---|
| RPi server | Python, FastAPI, SQLite |
| Dashboard UI | Next.js 15 + Tailwind (andrew.by/watch on Vercel) |
| Dashboard auth | Single password → signed cookie (Next.js middleware) |
| RPi exposure | Tailscale Funnel (public HTTPS) |
| Windows service | Python, pywin32, psutil, requests |
| Gatekeeper UI | tkinter (fullscreen overlay) |
| Packaging | PyInstaller → single exe |
| Networking | Tailscale |
| Anki completion | AnkiConnect REST API |
| Web active-time | pywin32 foreground window + input hooks |
| Web policy | dnsmasq on RPi (allowlist) + Windows Firewall DNS lock |
| Remote updates | Version manifest on RPi → Windows polls + self-replaces |
