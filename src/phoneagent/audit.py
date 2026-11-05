"""Append-only audit log of every tool call (private file, one JSON object per line)."""
import json
import os
import time
from pathlib import Path

PATH = Path(os.environ.get('PHONE_AGENT_HOME', Path.home() / '.phone-agent')) / 'audit.jsonl'


def log(tool, args, ok, message=None):
    try:
        PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(json.dumps(dict(ts=round(time.time(), 3), tool=tool, args=args, ok=ok, message=message)) + '\n')
    except OSError:
        pass  # auditing must never break the tool call


def tail(n=30):
    try:
        lines = PATH.read_text().splitlines()[-max(1, min(n, 500)):]
    except OSError:
        return []
    out = []
    for line in lines:
        try: out.append(json.loads(line))
        except ValueError: pass
    return out
