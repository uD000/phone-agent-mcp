"""Read-only access to the Mac's Contacts, for recipient lookup. Names and numbers stay in memory only.

The agent may only act on an EXACT name (apart from case and punctuation). Look-alikes are suggestions to show the
human; a typo or partial name must be confirmed by the human, or the human gives the number.
"""
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .names import is_exact, suggest
from .services.common import HOME, private_dir

APP = Path(__file__).resolve().parents[2] / 'bin' / 'PhoneAgentContacts.app'
TTL = 300
DENIED = ('Contacts access is not granted. Allow "PhoneAgentContacts" in System Settings > Privacy & Security > Contacts, '
          'or ask the human for the number.')


class Contacts:
    def __init__(self):
        self._rows, self._at, self._lock = None, 0.0, threading.Lock()

    def load(self, refresh=False):
        with self._lock:
            if self._rows is not None and not refresh and time.monotonic() - self._at < TTL: return self._rows
            if not APP.exists(): raise OSError('Contacts helper missing: see tools/contacts.swift and the README')
            out = private_dir(HOME / 'tmp') / ('contacts-%s.json' % uuid.uuid4().hex)
            status = Path(str(out) + '.status')
            try:
                # An .app bundle (unlike a bare binary) can show macOS's one-time permission prompt; wait for the answer.
                subprocess.run(['open', '-n', '-W', str(APP), '--args', str(out)], capture_output=True, timeout=150)
                state = status.read_text().strip() if status.exists() else ''
                if state == 'denied': raise OSError(DENIED)
                if not state.startswith('ok') or not out.exists(): raise OSError('Contacts export failed (%s)' % (state or 'helper did not run'))
                self._rows, self._at = json.loads(out.read_text()), time.monotonic()
            finally:
                for f in (out, status):
                    try: f.unlink()
                    except OSError: pass
            return self._rows

    def search(self, query, limit=8, extra=None):
        """Suggestions without numbers: name, exact, score, source, and how many numbers the Mac's Contacts has for it.
        `extra` is the names-only list scanned from the phone itself."""
        try:
            rows = self.load()
        except OSError:
            if not extra: raise
            rows = []
        by_name = {}
        for r in rows: by_name.setdefault(r['name'], []).append(r)
        mac = set(by_name)
        phone = {n for n in (extra or [])}
        pool = list(mac | phone)
        out = []
        for s in suggest(query, pool, limit):
            n = s['name']
            out.append(dict(s, source='both' if n in mac and n in phone else ('mac' if n in mac else 'phone'),
                            numbers=sum(len(r['phones']) for r in by_name.get(n, []))))
        return out

    def numbers(self, name):
        """Numbers for an exact name only."""
        hits = [r for r in self.load() if is_exact(name, r['name'])]
        if not hits:
            raise ValueError('%r is not an exact contact name. Ask the human to confirm one of the suggestions from '
                             'search_contacts, or to give the number.' % name)
        return [dict(name=r['name'], phones=r['phones']) for r in hits]
