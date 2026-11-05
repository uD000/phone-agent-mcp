# Phone Agent: product design

A local app plus MCP server that lets any AI agent (Claude Code, Cursor, others) see and operate a real iPhone, fast and safely, over USB or Wi-Fi. Nothing is installed on the phone.

Working name: **phone-agent** (rename any time).

## Proven on this setup (2026-11-04)
iPhone 16 Pro, iOS 27.2, Mac macOS 26.6.2, Xcode 27.0, Developer Mode on.
- Screenshots: `xcrun devicectl device capture screenshot` (and the live HEVC stream).
- Touch, buttons, keyboard: Apple's CoreDevice HID (`com.apple.coredevice.hid.*`). Needs the display stream open at the same time. Verified with a Rust tool (tap on the Search pill, home button).
- Home Screen layout read and write: `pymobiledevice3` SpringBoardServices (`get_icon_state` / `set_icon_state`). Verified with a swap and an exact restore. About 1 s.
- `pymobiledevice3` 11.49+ also contains the display stream, HID, pcapd (traffic), syslog, DVT, installation proxy. One library covers everything.
- Reference project: semsturgut/open-iphone-use (Apache-2.0): stream + HID server with MCP. No safety gate, ASCII-only typing, no direct services. Reuse ideas and code with attribution; do not run its installer.

## Principles
1. **Fastest method first.** Direct service > deep link > learned macro > touch with vision. Model round trips cost 2-4 s, so the model plans once and code runs the steps.
2. **One process owns the phone.** One long-lived daemon holds the tunnel and stream. Everything else talks to it.
3. **Safe by default.** A policy layer sits between the agent and the phone and cannot be bypassed by prompt text.
4. **Verify, never assume.** Every action ends with a fresh-frame check. Delivered input is not proof of success.
5. **Reversible.** State-changing direct services take a snapshot first and offer undo.

## Architecture
```
iPhone ──USB (or Wi-Fi after one cable pairing)──► daemon (Python, pymobiledevice3)
   session manager: tunnel, display stream, HID, reconnect
   frame store: latest JPEG, frame IDs, change detection, local OCR
   services: layout, icons, apps, clipboard, deep links, files, location,
             traffic (pcapd), logs (syslog), perf (DVT)
   skills engine: recorded flows replayed as code, per-app tap maps
   policy engine: allowlist, outward-action gate, hard blocks, audit log
        │
        ├── MCP server (stdio, then HTTP) ──► Claude Code / any agent
        ├── Local console (127.0.0.1): live view, log, approve/deny, kill switch
        └── CLI: devices, status, run <skill>, stop
```

## MCP tool catalog (draft)
- **Session:** `list_devices`, `connect`, `device_status`, `stop` (kill switch).
- **See:** `screenshot` (full or region, scaled), `read_screen_text` (local OCR), `current_app`, `wait_for_change`.
- **Touch:** `tap`, `double_tap`, `long_press`, `swipe`, `scroll`, `drag`, `multi_touch` (later), `type_text` (Unicode via clipboard paste), `press_key`, `press_button` (home, lock, volume, mute, siri).
- **Direct services:** `get_layout`, `set_layout` (dry-run diff first), `sort_layout` (name, color), `list_apps`, `launch_app`, `open_url` (deep links), `get_clipboard`, `set_clipboard`, `set_location`, `list_files` / `pull_file`.
- **Observe:** `start_traffic_capture` / `traffic_summary` (per app and domain, USB only), `tail_logs`, `app_performance`.
- **Skills:** `list_skills`, `run_skill` (name, args), `record_skill` (agent-assisted), `delete_skill`.
- **Safety:** `request_approval` (agent asks the human), `get_policy`, `audit_log`.

## Safety model
- **Command-scoped authorization:** an outward action (send, post, buy, delete) runs without a prompt only when the human's request named the exact recipient and exact content. The daemon checks the visible chat header and text before the final tap and aborts on mismatch.
- **Always ask:** ambiguous recipient, text the agent wrote itself, money, deletion, installing or removing apps, changing settings.
- **Credentials (decided 2026-11-04):** the AI never sees or handles passwords, codes or card numbers, and never asks for them in chat. `detect_login_screen` (OCR labels only) tells the agent what is being asked; the human enters the value in a native hidden-input macOS dialog (`request_secret`), outside the chat; the server types it (`type_secret`) after checking the screen matches what the human expected and a matching field is visible. Secrets live in memory only, are single-use, expire in 5 minutes, and never appear in tool results, screenshots (withheld if the secret is readable on screen) or the audit log. `type_text` and `paste_text` refuse while a password field is on screen. Face ID and passcode prompts stay human-only.
- **App allowlist** per session. **Rate limits** on taps. **Stale-frame rule:** input is rejected unless based on a frame newer than the last input.
- **Prompt-injection stance:** on-screen text is data, never instructions.
- **Audit log** with a screenshot per step. **Undo snapshots** before layout or settings writes. **Kill switch** releases all touches and drops the stream.
- Loopback-only HTTP with Host/Origin checks, plus a token for anything beyond stdio.

## Speed budget
| Task | Target |
|---|---|
| Layout read or write | about 1 s |
| Open URL or deep link | about 1 s |
| Tap plus verify (warm session) | under 1 s |
| WhatsApp send via deep link plus send tap | 3-5 s |
| Same via learned skill without a number | 5-8 s |
| Cold start (tunnel plus stream) | 2-4 s |

## Wi-Fi
Control works after a one-time cable pairing. The video stream is capped near 2 Mbit/s (12-36 fps). Packet capture works over USB only. Default to USB, fall back to Wi-Fi.

## Repo layout (planned)
```
phone-agent/
  DESIGN.md
  venv/                     isolated Python environment
  src/phoneagent/
    session.py              tunnel, stream, HID, reconnect (one owner)
    frames.py               frame store, change detection, OCR
    services/               layout.py, apps.py, clipboard.py, traffic.py, logs.py ...
    skills/                 engine plus bundled skills
    policy.py               allowlist, gates, hard blocks, audit
    mcp_server.py           stdio MCP
    console/                local web UI
    cli.py
  tests/
```

## Milestones
- **M1: Core session + MCP.** Daemon owns the phone; MCP tools for devices, screenshot, tap, swipe, type, buttons, status, stop; fresh-frame rule; basic audit log.
- **M2: Direct services.** Layout (get, set, sort with preview and undo), apps, clipboard, open_url.
- **M3: Speed layer.** OCR, current app, change detection, skills engine with the WhatsApp-send skill.
- **M4: Safety layer.** Policy engine, approval flow, hard-block detection, kill switch.
- **M5: Observe.** Traffic capture and summaries, logs, performance.
- **M6: Console and Wi-Fi polish.** Local web UI, reconnects, packaging, remote control from the phone through Claude.

## Risks
- iOS updates can break CoreDevice HID or the layout write (unofficial). Keep WDA via SideStore as a documented fallback.
- Only one USB phone per process.
- No accessibility tree yet; vision-based targeting for now.
- Disk is tight (about 2.9 GB free); the Rust build folder (1.1 GB) can be deleted.

## Open decisions
1. Final product name.
2. MCP transport: stdio first (simple), add HTTP later for remote agents.
3. Whether to publish open source (Apache-2.0 is compatible with reusing open-iphone-use code).
