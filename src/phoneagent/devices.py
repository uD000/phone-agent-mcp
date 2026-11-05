"""Device registry: several phones (iPhones, iPads, Android) at once, each with its own session and safety state."""
import threading

from . import policy


class DeviceState:
    """Everything that belongs to ONE device. The kill switch, policy and credentials store are global."""

    def __init__(self, udid, name, platform, session, ops):
        self.udid, self.name, self.platform, self.session, self.ops = udid, name, platform, session, ops
        self.quarantine = {}                 # set after a plain-text secret is typed here: screenshots held until the page changes
        self.last_reason = ['']
        self.busy = None                     # set while a long background job (a contacts scan) is driving this device
        self.limiter = policy.RateLimiter()

    def label(self): return '%s (%s)' % (self.name, self.platform)


class Registry:
    def __init__(self):
        self.states, self.lock = {}, threading.Lock()

    def add(self, state):
        with self.lock: self.states[state.udid] = state
        return state

    def get(self, udid): return self.states.get(udid)

    def find(self, ident):
        """Match by udid, exact name, or a unique prefix of either (case-insensitive)."""
        want = ident.strip().casefold()
        with self.lock: states = list(self.states.values())
        for s in states:
            if want in (s.udid.casefold(), (s.name or '').casefold()): return s
        hits = [s for s in states if s.udid.casefold().startswith(want) or (s.name or '').casefold().startswith(want)]
        if len(hits) == 1: return hits[0]
        raise ValueError('%s device %r. Known devices: %s' % ('Ambiguous' if hits else 'Unknown', ident,
                                                             ', '.join(s.label() for s in states) or 'none (call connect)'))

    def all(self):
        with self.lock: return list(self.states.values())
