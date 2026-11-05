"""Screen text recognition on the Mac (Apple Vision, via the bin/ocr helper kept running between calls)."""
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from .services.common import HOME, private_dir

BIN = Path(__file__).resolve().parents[2] / 'bin' / 'ocr'


def norm(text):
    return re.sub(r'[^\w\s]', '', text.casefold()).strip()


class Ocr:
    def __init__(self):
        self.proc, self.lock = None, threading.Lock()

    def _start(self):
        if not BIN.exists(): raise OSError('OCR helper missing: build it with swiftc tools/ocr.swift -o bin/ocr')
        self.proc = subprocess.Popen([str(BIN), '--serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)

    def read(self, jpeg, min_confidence=0.4):
        """Recognized lines: [{text, confidence, box:[x,y,w,h], words:[{text, box}]}], coordinates 0..1 top-left."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None: self._start()
            directory = private_dir(HOME / 'tmp')
            fd, path = tempfile.mkstemp(suffix='.jpg', dir=directory)
            try:
                with os.fdopen(fd, 'wb') as f: f.write(jpeg)
                self.proc.stdin.write('%s\t%s\n' % (min_confidence, path)); self.proc.stdin.flush()
                reply = json.loads(self.proc.stdout.readline() or '{"error":"OCR helper stopped"}')
            finally:
                try: os.unlink(path)
                except OSError: pass
        if isinstance(reply, dict): raise OSError(reply.get('error', 'OCR failed'))
        return reply


def center(box):
    return round(box[0] + box[2] / 2, 4), round(box[1] + box[3] / 2, 4)


def find(lines, query, exact=False, min_y=0.0, max_y=1.0):
    """Where does this text appear? Matches whole words in a line (phrase), else a substring of a line."""
    wanted = norm(query).split()
    if not wanted: raise ValueError('Empty query')
    found = []
    for line in lines:
        cy = line['box'][1] + line['box'][3] / 2
        if not (min_y <= cy <= max_y): continue                # only look in this band of the screen
        words = [norm(w['text']) for w in line.get('words', [])]
        hit = None
        for i in range(len(words) - len(wanted) + 1):
            if words[i:i + len(wanted)] == wanted:
                boxes = [w['box'] for w in line['words'][i:i + len(wanted)]]
                x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
                x1, y1 = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
                hit = [x0, y0, x1 - x0, y1 - y0]
                break
        if exact:
            hit = line['box'] if norm(line['text']) == ' '.join(wanted) else None
        elif hit is None and norm(' '.join(wanted)) in norm(line['text']): hit = line['box']
        if hit is not None:
            x, y = center(hit)
            found.append(dict(text=line['text'], x=x, y=y, box=[round(v, 4) for v in hit]))
    return sorted(found, key=lambda m: (round(m['y'], 2), m['x']))


def digits_on_screen(lines, wanted):
    """Is this digit string (a phone number, say) visible, ignoring spaces, dashes and plus signs?"""
    target = re.sub(r'\D', '', wanted)
    return bool(target) and any(target in re.sub(r'\D', '', line['text']) for line in lines)


def overlap(lines, text):
    """Share of the words in text that appear somewhere on screen (0..1), for long or partly misread text."""
    wanted = [w for w in norm(text).split() if w]
    if not wanted: return 1.0
    seen = set(norm(' '.join(l['text'] for l in lines)).split())
    return sum(1 for w in wanted if w in seen) / len(wanted)


def compact(lines):
    return [dict(text=l['text'], x=center(l['box'])[0], y=center(l['box'])[1], conf=round(l['confidence'], 2)) for l in lines]


# ---- login / credential screens -------------------------------------------------------------------
CUES = {
    'password': ['password', 'passcode', 'passwort', 'mot de passe', 'contrasena'],
    'otp': ['verification code', 'one-time', 'one time', 'otp', 'enter the code', 'enter code', '6-digit', '6 digit',
            '2-step', 'two-step', 'security code', 'authenticator'],
    'payment': ['card number', 'cvv', 'cvc', 'expiry', 'expiration date', 'security code'],
    'username': ['email or phone', 'phone or email', 'email address', 'username', 'user name', 'user id', 'email',
                 'mobile number', 'phone number', 'apple id'],
    'sign_in': ['sign in', 'log in', 'login', 'continue with google', 'sign up', 'welcome back'],
}
PRIORITY = ['password', 'otp', 'payment', 'username', 'sign_in']
SERVICES = ['google', 'apple', 'microsoft', 'facebook', 'instagram', 'amazon', 'netflix', 'spotify', 'github', 'x', 'paypal']


def classify_screen(lines):
    """What kind of credential, if any, is this screen asking for? Looks at labels only; never at typed values."""
    text = [norm(l['text']) for l in lines]
    found = {}
    for kind, words in CUES.items():
        hits = sorted({w for w in words for t in text if re.search(r'(^|\s)%s(\s|$)' % re.escape(w), t)})
        if hits: found[kind] = hits
    kind = next((k for k in PRIORITY if k in found), None)
    services = [s for s in SERVICES if any(re.search(r'(^|\s)%s(\s|$)' % s, t) for t in text)]
    return dict(kind=kind, human_input_needed=kind in ('password', 'username', 'otp', 'payment'), cues=found, services=services)


def secret_lines(lines, secret):
    """Indexes of recognized lines that look like (part of) the secret. OCR misreads random strings, so this is fuzzy:
    shared 4-character runs, a long common block, or a similar whole line all count."""
    from difflib import SequenceMatcher
    flat = re.sub(r'[^0-9a-z]', '', secret.casefold())
    if not flat: return []
    hits = []
    for i, line in enumerate(lines):
        text = re.sub(r'[^0-9a-z]', '', line['text'].casefold())
        if len(text) < 3: continue
        matcher = SequenceMatcher(None, text, flat, autojunk=False)
        block = matcher.find_longest_match(0, len(text), 0, len(flat)).size
        window = min(4, len(flat))
        if (any(flat[k:k + window] in text for k in range(len(flat) - window + 1))
                or block >= max(3, (len(flat) + 1) // 2) or matcher.ratio() >= .6):
            hits.append(i)
    return hits


def secret_visible(lines, secret):
    return bool(secret_lines(lines, secret))


HUMAN_ONLY = ['enter passcode', 'enter your passcode', 'face id', 'touch id', 'confirm with side button',
              'double click to pay', 'enter apple id password']


def human_only_screen(lines):
    """Passcode and biometric prompts: only the human may act here."""
    text = [norm(l['text']) for l in lines]
    return next((p for p in HUMAN_ONLY if any(p in t for t in text)), None)


def label_at(lines, x, y, margin=.012):
    """Text of the smallest recognized line whose box contains the point (x, y), else None."""
    best = None
    for l in lines:
        bx, by, bw, bh = l['box']
        if bx - margin <= x <= bx + bw + margin and by - margin <= y <= by + bh + margin:
            if best is None or bw * bh < best[0]: best = (bw * bh, l['text'])
    return best[1] if best else None
