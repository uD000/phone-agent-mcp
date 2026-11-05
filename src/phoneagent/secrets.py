"""Credentials the AI never sees.

The human enters a secret in a native macOS hidden-input dialog (outside the chat). It lives in memory only,
is typed into the phone by the server, and is never returned in a tool result, a screenshot or the audit log.
A secret is single-use by default, expires, and is only typed when the screen matches what the human expected.
"""
import secrets as _secrets
import subprocess
import threading
import time

FIELDS = ('password', 'username', 'email', 'code', 'other')


def _quote(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ') + '"'


class Secrets:
    def __init__(self):
        self._items, self._recent, self._lock = {}, [], threading.Lock()

    def ask(self, label, field, expect, uses=1, ttl=300, timeout=180):
        """Show the hidden-input dialog. Returns an opaque id. Raises ValueError if the human cancels."""
        if field not in FIELDS: raise ValueError('field must be one of: ' + ', '.join(FIELDS))
        where = (' It will only be typed if the phone screen shows: %s.' % expect) if expect else ''
        prompt = ('An AI agent needs: %s (%s).%s\n\nType or paste it here. It goes straight to the phone and the AI '
                  'never sees it. Cancel to refuse.') % (label, field, where)
        script = ('set r to display dialog %s default answer "" with hidden answer with title "phone-agent: secret '
                  'entry" with icon caution buttons {"Cancel", "Use it once"} default button 2 giving up after %d\n'
                  'if gave up of r then error number -128\nreturn text returned of r') % (_quote(prompt), timeout)
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=timeout + 15)
        if result.returncode != 0:
            raise ValueError('The human cancelled or did not answer: no secret was provided. Ask them to give the number or '
                             'enter it themselves.')
        value = result.stdout[:-1] if result.stdout.endswith('\n') else result.stdout
        if not value: raise ValueError('The human submitted an empty value')
        sid = 's_' + _secrets.token_hex(6)
        with self._lock:
            self._items[sid] = dict(label=label, field=field, expect=expect, value=value, uses=uses,
                                    expires=time.monotonic() + ttl)
        return sid, len(value)

    def peek(self, sid):
        with self._lock:
            self._purge()
            item = self._items.get(sid)
            if not item: raise ValueError('Unknown or expired secret id. Ask again with request_secret.')
            return dict(item)

    def use(self, sid):
        with self._lock:
            item = self._items.get(sid)
            if item:
                self._recent.append((item['value'], time.monotonic()))
                item['uses'] -= 1
                if item['uses'] <= 0: self._items.pop(sid, None)

    def recent_values(self, ttl=45):
        """Secrets typed in the last ttl seconds: any screenshot returned meanwhile must not show them."""
        with self._lock:
            now = time.monotonic()
            self._recent = [(v, t) for v, t in self._recent if now - t < ttl]
            return [v for v, _ in self._recent]

    def forget_all(self):
        with self._lock:
            n = len(self._items)
            self._items.clear()
            self._recent.clear()
            return n

    def _purge(self):
        now = time.monotonic()
        for sid in [k for k, v in self._items.items() if v['expires'] < now]: self._items.pop(sid, None)
