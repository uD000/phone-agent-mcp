"""Human-controlled policy. The file is edited by the human (CLI or editor); the agent can only read it.

approval: "dialog" (default) = every outward action needs the human to click Allow in a native dialog the agent cannot
click; the agent's own approved=true is ignored. "agent" = trust the agent's approved flag (faster, weaker).
"""
import json
import os
import re
import time

from .services.common import HOME, private_dir

PATH = HOME / 'policy.json'
DEFAULT = {
    'approval': 'dialog',
    'app_allowlist': None,          # list of bundle ids the agent may launch; null = any
    'app_denylist': [],             # bundle ids the agent may never launch
    'precheck': True,               # OCR the screen before taps: block passcode prompts, gate Send/Pay/Delete taps
    'max_inputs_per_minute': 90,
    'outward_labels': ['send', 'post', 'pay', 'buy', 'order', 'place order', 'purchase', 'checkout', 'confirm payment',
                       'delete', 'remove', 'transfer', 'subscribe', 'submit', 'reply', 'share', 'tweet', 'sign out'],
}
_cache = {'mtime': None, 'value': None}


def load():
    try:
        mtime = PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _cache['value'] is not None and _cache['mtime'] == mtime: return _cache['value']
    policy = dict(DEFAULT)
    if mtime is not None:
        try:
            policy.update({k: v for k, v in json.loads(PATH.read_text()).items() if k in DEFAULT})
        except (OSError, ValueError):
            policy['approval'] = 'dialog'   # unreadable file: stay strict
    if policy['approval'] not in ('dialog', 'agent'): policy['approval'] = 'dialog'
    _cache.update(mtime=mtime, value=policy)
    return policy


def save(policy):
    private_dir(PATH.parent)
    PATH.write_text(json.dumps({k: policy[k] for k in DEFAULT}, indent=2))
    PATH.chmod(0o600)
    _cache['value'] = None


def app_allowed(bundle_id):
    p = load()
    if bundle_id in p['app_denylist']: return False, 'is on the human\'s deny list'
    if p['app_allowlist'] is not None and bundle_id not in p['app_allowlist']:
        return False, 'is not on the human\'s allow list'
    return True, ''


def outward_label(text):
    """Is this on-screen label a send/pay/delete style button?"""
    t = re.sub(r'[^\w\s]', '', text.casefold()).strip()
    return t if t in {l.casefold() for l in load()['outward_labels']} else None


class RateLimiter:
    def __init__(self): self.times = []

    def hit(self):
        now = time.monotonic()
        self.times = [t for t in self.times if now - t < 60]
        if len(self.times) >= load()['max_inputs_per_minute']: return False
        self.times.append(now)
        return True
