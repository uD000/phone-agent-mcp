"""Phone clipboard through devicectl (works over USB and Wi-Fi)."""
from .common import run_devicectl

TEXT = 'public.utf8-plain-text'


def set_text(udid, text):
    if len(text.encode()) > 100_000: raise ValueError('Text too large for the clipboard')
    run_devicectl(udid, 'pasteboard', 'copy', '--type', TEXT, stdin=text, timeout=30)


def get_text(udid):
    return run_devicectl(udid, 'pasteboard', 'paste', timeout=30)
