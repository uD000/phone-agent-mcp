"""Shared helpers for direct phone services (no screen stream needed)."""
import os
import subprocess
from pathlib import Path

HOME = Path(os.environ.get('PHONE_AGENT_HOME', Path.home() / '.phone-agent'))


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


async def lockdown(udid):
    """Lockdown client over USB or Wi-Fi (usbmuxd), using the Mac's existing pairing."""
    from pymobiledevice3.lockdown import create_using_usbmux
    return await create_using_usbmux(serial=udid, autopair=False)


def run_devicectl(udid, group, action, *extra, stdin=None, timeout=60):
    command = ['xcrun', 'devicectl', 'device', group, action, '--device', udid, '--quiet', *extra]
    result = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout).strip()
                      else 'devicectl failed')
    return result.stdout
