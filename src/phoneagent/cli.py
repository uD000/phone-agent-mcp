"""phone-agent command line (human side).

  serve                    run the daemon (one process owns the phone; MCP at /mcp, console at /)
  console                  open the console in your browser
  status                   daemon and phone status
  token                    where the access token lives (agents need it for http MCP)
  kill | resume            emergency stop / continue (pa-kill, pa-resume do the same)
  policy [show | set K V]  your safety policy
  audit [N]                recent actions
  doctor                   check this Mac, Xcode, devices and helpers, and say what is missing
"""
import json
import sys

from . import audit, policy


def _api(path):
    import urllib.request
    from . import daemon
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (daemon.PORT, path), headers={'Authorization': 'Bearer ' + daemon.token()})
    with urllib.request.urlopen(req, timeout=5) as r: return json.loads(r.read())


def doctor():
    """Print what works and what is missing on this Mac. Read-only: it never touches a phone beyond listing it."""
    import importlib.metadata as md
    import platform
    import re
    import shutil
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    bad = []

    def line(ok, text, hint=''):
        print(('  ok   ' if ok is True else '  WARN ' if ok is None else '  FAIL ') + text + (('  -> ' + hint) if hint and ok is not True else ''))
        if ok is False: bad.append(text)

    def run(*cmd, timeout=25):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as error:
            return 1, str(error)
    print('This Mac')
    line(platform.system() == 'Darwin', 'macOS %s' % platform.mac_ver()[0], 'phone-agent needs macOS (it uses Xcode\'s device services)')
    line(sys.version_info >= (3, 11), 'Python %s' % platform.python_version(), 'Python 3.11 or newer is required')
    code, out = run('xcodebuild', '-version')
    m = re.search(r'Xcode (\d+)', out)
    line(code == 0 and bool(m) and int(m.group(1)) >= 27 if code == 0 and m else False,
         'Xcode ' + (out.splitlines()[0].split()[-1] if code == 0 and out else 'not found'),
         'Xcode 27 or newer, opened once with the phone connected (it installs the developer tools on the phone)')
    code, out = run('xcrun', 'devicectl', '--version')
    line(code == 0, 'devicectl ' + (out.splitlines()[-1] if code == 0 and out else 'missing'), 'part of Xcode')
    print('Python packages')
    for pkg in ('pymobiledevice3', 'mcp', 'Pillow'):
        try: line(True, '%s %s' % (pkg, md.version(pkg)))
        except md.PackageNotFoundError: line(False, pkg + ' missing', 'run ./setup.sh')
    print('Helpers')
    line((root / 'bin' / 'ocr').exists(), 'screen-text helper bin/ocr', 'build with: swiftc -O tools/ocr.swift -o bin/ocr (or ./setup.sh)')
    line(True if (root / 'bin' / 'PhoneAgentContacts.app').exists() else None, 'Contacts helper (optional)', 'build with: sh tools/build_contacts_app.sh')
    line(True if shutil.which('adb') else None, 'adb for Android (optional)', 'brew install android-platform-tools')
    print('Devices')
    code, out = run('xcrun', 'devicectl', 'list', 'devices')
    rows = [l for l in out.splitlines() if 'iPhone' in l or 'iPad' in l or 'paired' in l]
    line(bool(rows), '%d iPhone/iPad known to Xcode' % len(rows), 'connect the phone by USB, unlock it and tap Trust')
    for r in rows[:3]: print('         ' + re.sub(r'\s{2,}', '  ', r.strip())[:100])
    if rows:
        udids = re.findall(r'([0-9A-F]{8}-[0-9A-F]{16}|[0-9A-F]{8}-\d{4}-[0-9A-F-]{20,})', out)
        code, info = run('xcrun', 'devicectl', 'device', 'info', 'details', '--device', udids[0]) if udids else (1, '')
        dm = re.search(r'Developer Mode Status: (\w+)', info)
        line(bool(dm) and dm.group(1) == 'Enabled', 'Developer Mode on the phone: ' + (dm.group(1) if dm else 'unknown'),
             'Settings > Privacy & Security > Developer Mode')
        for cap in ('HID Digitizer', 'View Device Screen', 'Capture Screenshot'):
            line(cap in info, 'phone offers: ' + cap, 'needs iOS 27 and Xcode 27; open Xcode once with the phone connected')
    if shutil.which('adb'):
        code, out = run('adb', 'devices')
        found = [l.split()[0] + ' ' + l.split()[1] for l in out.splitlines()
                 if '\t' in l or (len(l.split()) == 2 and l.split()[1] in ('device', 'unauthorized', 'offline'))]
        print('         adb: ' + (', '.join(found) or 'no Android devices'))
    print()
    print('Everything needed is in place.' if not bad else 'Fix the FAIL lines above, then run `phone-agent doctor` again.')
    return 1 if bad else 0


def main(argv):
    if argv[:1] == ['doctor']: return doctor()
    if argv[:1] == ['serve']:
        from . import daemon; daemon.main(); return 0
    if argv[:1] == ['console']:
        from . import daemon; daemon.open_console(); return 0
    if argv[:1] == ['token']:
        from . import daemon; daemon.token(); print(daemon.TOKEN_PATH); return 0
    if argv[:1] == ['status']:
        try: print(json.dumps(_api('/api/status'), indent=2))
        except OSError as error: print('daemon not reachable (%s). Start it with: phone-agent serve' % error); return 1
        return 0
    if argv[:1] in (['kill'], ['resume']):
        from . import killswitch
        if argv[0] == 'kill': killswitch.FLAG.parent.mkdir(mode=0o700, parents=True, exist_ok=True); killswitch.FLAG.touch(mode=0o600); print('kill switch ON')
        else: killswitch.FLAG.unlink(missing_ok=True); print('kill switch OFF')
        return 0
    if len(argv) >= 1 and argv[0] == 'policy':
        p = policy.load()
        if len(argv) == 1 or argv[1] == 'show':
            print(json.dumps(p, indent=2)); return 0
        if argv[1] == 'set' and len(argv) >= 4:
            key, value = argv[2], ' '.join(argv[3:])
            if key not in policy.DEFAULT: print('Unknown key. Keys:', ', '.join(policy.DEFAULT)); return 2
            p[key] = json.loads(value) if value.strip()[:1] in '[{0123456789' or value in ('null', 'true', 'false') else value
            policy.save(p); print(json.dumps(policy.load(), indent=2)); return 0
        print('usage: policy [show | set KEY VALUE]   (VALUE is JSON for lists/numbers, e.g. set app_allowlist \'["com.apple.Preferences"]\')')
        return 2
    if len(argv) >= 1 and argv[0] == 'audit':
        n = int(argv[1]) if len(argv) > 1 else 30
        for row in audit.tail(n): print(json.dumps(row))
        return 0
    print(__doc__); return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))


def entry():
    sys.exit(main(sys.argv[1:]))
