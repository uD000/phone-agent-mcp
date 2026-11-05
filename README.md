# phone-agent-mcp

Let an AI agent (Claude Code, or any MCP client) **see and operate a real phone** from your Mac: iPhones and iPads over USB or
Wi-Fi, Android phones and Android TVs over adb. Nothing is installed on the phone. On iOS it uses the same device services Xcode
uses for screen mirroring (CoreDevice), through [pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

It is a local MCP server (stdio), with an optional shared daemon and a small web console: a live view of the phone, the action
log, and a kill switch.

> **Unofficial.** This relies on undocumented Apple services that can change in any iOS or Xcode update. It is not affiliated with
> or endorsed by Apple, Google or WhatsApp. Use it only on devices you own. Automating a messaging app may be against that app's
> terms of service: that is your responsibility. Read the safety notes below before letting an agent near your phone.

## What it can do

| Area | Tools |
|---|---|
| Sessions | `list_devices`, `connect`, `disconnect`, `device_status`, `stop` (several devices at once; every tool takes `device`) |
| See | `screenshot`, `read_screen_text` (on-device OCR), `find_text`, `wait_for_text`, `wait_for_change` |
| Act | `tap` (with `hold`), `tap_text`, `swipe`, `drag`, `type_text`, `press_key`, `press_button` |
| Apps | `list_apps`, `launch_app`, `open_url` (deep links) |
| iPhone extras | Home Screen layout (`get_layout`, `sort_layout` with preview and undo), clipboard, `paste_text` |
| Skills | `run_skill`, `save_skill`: replayable flows. Bundled: `open_app`, `whatsapp_send`, `whatsapp_send_to_contact` |
| Credentials | `detect_login_screen`, `request_secret`, `type_secret`: you type secrets in a private dialog; the AI never sees them |
| Observe | `network_traffic` (per app over USB), `tail_logs`, `app_performance`, `battery_status`, `device_info` |
| Contacts (optional) | `search_contacts`, `get_contact_numbers` (the Mac's Contacts, exact names only) |
| Safety | `get_policy`, `audit_log` |

Android TV has no touch: the agent navigates with `press_key` (up, down, left, right, select, back, home, play_pause).

## Requirements

- A Mac with **macOS 26+** and **Xcode 27+**, opened once with the phone connected (it installs the developer tools on the phone).
- iPhone/iPad on **iOS 27+**, trusted by the Mac, with **Developer Mode** on (Settings > Privacy & Security).
- Python 3.11+, and `swiftc` (comes with Xcode) to build the screen-text helper.
- Optional: `adb` for Android (`brew install android-platform-tools`).

## Install

```bash
git clone https://github.com/uD000/phone-agent-mcp.git
cd phone-agent-mcp
./setup.sh                       # venv, dependencies, helpers, then a health check
```

`./setup.sh` ends by running `venv/bin/phone-agent doctor`, which tells you exactly what is missing (Xcode version, Developer
Mode, the phone's screen and touch services, helpers). Then register the server with Claude Code and start a **new** session:

```bash
claude mcp add phone-agent -- "$PWD/venv/bin/python" -m phoneagent.mcp_server
```

Ask it to *"list my phones"*, then *"take a screenshot"*.

### Check it works

```bash
venv/bin/python tests/test_offline.py        # no phone needed
venv/bin/python tests/test_protocol.py       # starts the real MCP server, no phone needed
venv/bin/python scripts/smoke_live.py        # your phone: connect, screenshot, OCR, Home. Sends nothing.
```

## Live view and controls

The MCP server hosts a console on `127.0.0.1:47831`. Open it with a single-use login link (your token never goes in a URL):

```bash
venv/bin/phone-agent console
```

It shows the live screen (up to the ~55 fps the phone's video offers), the action log, the policy, a Wi-Fi quality selector and the
**kill switch**. For several agents sharing one phone session, run `venv/bin/phone-agent serve` and point clients at the daemon.

## Safety model

All of this is controlled by **you**, not the agent:

- **Approvals.** Anything that sends, posts, buys or deletes needs your click in a native macOS dialog. The agent cannot click it
  and its own `approved=true` is ignored. If you deny or do not answer, nothing happens.
- **Credentials.** Passwords, codes and card numbers are entered by you in a private dialog, typed into the phone by the server,
  and never appear in tool results, screenshots (withheld if a secret is readable on screen) or the audit log.
- **Kill switch.** `bin/pa-kill` (or the console button) stops all input at once. Turning it off needs your confirmation.
- **Passcode / Face ID prompts** are human-only; taps and typing are refused while one is on screen.
- **Policy** in `~/.phone-agent/policy.json` (`phone-agent policy show|set`): approval mode, app allow/deny lists, rate limit.
- **Audit log** in `~/.phone-agent/audit.jsonl`: every tool call; never secrets, clipboard text or traffic results.
- The console/daemon listens on loopback only, checks the Host header, and needs a bearer token or a single-use code.

**Honest limits.** These guards stop what an agent can do *through the tools*. An agent that also has unrestricted shell access on
the same Mac could edit `~/.phone-agent/policy.json` or delete the kill-switch file: run untrusted agents in a sandbox. The app
allow-list only governs launching apps, not where taps land. Packet capture needs a USB cable (iOS refuses it over Wi-Fi). The
screen stream can freeze on Wi-Fi; the session detects that and rebuilds it. See `DESIGN.md` for the full design.

## Layout

`src/phoneagent/`: `session.py` (tunnel, stream, input) · `mcp_server.py` (tools) · `console_app.py`, `daemon.py` ·
`policy.py`, `approval.py`, `killswitch.py`, `secrets.py` · `services/` (layout, apps, clipboard, observe) · `skills/` ·
`vision.py` (OCR) · `android.py`. `tools/` holds the Swift helpers (built into `bin/` by `setup.sh`).

## License

GPL-3.0-or-later (see `LICENSE`), because it links against pymobiledevice3 (GPL-3.0). Portions are adapted from
[open-iphone-use](https://github.com/semsturgut/open-iphone-use) (Apache-2.0); see `NOTICE` and `licenses/`.
