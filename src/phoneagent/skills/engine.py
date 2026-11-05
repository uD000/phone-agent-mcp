"""Skills: named, parameterized step lists replayed as plain code (no model call per step).

A skill is JSON: {name, description, params: {name: {description, default?}}, steps: [{do, ...}]}.
Step values may use {param} and {param|urlencode|lower|digits}. A step marked "outward": true (it sends,
posts, buys or deletes something) does not run unless the caller passes approved=true: the run stops before
it and reports exactly what would happen.
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

from .. import android as android_svc, vision
from ..services import apps as apps_svc, clipboard as clip_svc, layout as layout_svc
from ..services.common import HOME, private_dir

BUNDLED = Path(__file__).parent / 'bundled'
USER = HOME / 'skills'
NAME = re.compile(r'^[a-z][a-z0-9_]{1,40}$')
TOKEN = re.compile(r'\{(\w+)(?:\|(\w+))?\}')
FILTERS = {'urlencode': lambda v: quote(v, safe=''), 'lower': str.lower, 'digits': lambda v: re.sub(r'\D', '', v)}
MAX_SECONDS = 120


class NeedsChoice(ValueError):
    """The skill cannot continue until the human chooses between options (for example WhatsApp Personal or Business)."""
    def __init__(self, question, options):
        super().__init__(question)
        self.question, self.options = question, options


def fill(value, params):
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in params: raise ValueError('Missing parameter ' + m.group(1))
            text = str(params[m.group(1)])
            if m.group(2):
                if m.group(2) not in FILTERS: raise ValueError('Unknown filter ' + m.group(2))
                text = FILTERS[m.group(2)](text)
            return text
        return TOKEN.sub(sub, value)
    if isinstance(value, list): return [fill(v, params) for v in value]
    if isinstance(value, dict): return {k: fill(v, params) for k, v in value.items()}
    return value


def load_all():
    skills = {}
    for directory, source in ((BUNDLED, 'bundled'), (USER, 'user')):
        if not directory.exists(): continue
        for path in sorted(directory.glob('*.json')):
            try:
                skill = json.loads(path.read_text())
                if skill.get('name') == path.stem and source == 'user' and path.stem in skills: continue
                skills[skill['name']] = dict(skill, source=source)
            except (OSError, ValueError, KeyError):
                continue
    return skills


def validate(skill):
    if not NAME.match(str(skill.get('name', ''))): raise ValueError('name must be lower_snake_case, 2-41 chars')
    steps = skill.get('steps')
    if not isinstance(steps, list) or not steps or len(steps) > 60: raise ValueError('steps must be a list of 1-60 steps')
    for n, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get('do') not in Ops.NAMES:
            raise ValueError('step %d: "do" must be one of %s' % (n, ', '.join(sorted(Ops.NAMES))))
    if not isinstance(skill.get('params', {}), dict): raise ValueError('params must be an object')


def save(skill):
    validate(skill)
    if skill['name'] in {p.stem for p in BUNDLED.glob('*.json')}: raise ValueError('That name belongs to a bundled skill')
    private_dir(USER)
    path = USER / (skill['name'] + '.json')
    path.write_text(json.dumps({k: v for k, v in skill.items() if k != 'source'}, indent=2))
    path.chmod(0o600)
    return path.name


def delete(name):
    path = USER / (name + '.json')
    if not NAME.match(name) or not path.exists(): raise ValueError('No user skill named %r' % name)
    path.unlink()


class Ops:
    """Step implementations on top of the live session and the direct services."""
    platform = 'ios'
    NAMES = {'open_url', 'whatsapp_open', 'whatsapp_launch', 'input_text', 'tap_beside_text', 'find_chat', 'assert_header', 'wait_absent', 'launch_app', 'tap_xy', 'tap_text', 'type_text', 'paste', 'press_key', 'press_button',
             'wait_text', 'assert_text', 'wait_change', 'sleep'}

    def __init__(self, session, ocr):
        self.session, self.ocr, self._apps = session, ocr, None

    def lines(self, min_confidence=.4, redact=True):
        """Recognized screen text. Lines that look like a secret typed in the last two minutes are redacted."""
        quiet = getattr(self.session, 'real_at', None)
        if quiet is not None and time.monotonic() - quiet > 2.5:                # the video has been silent: is the picture stale?
            try: self.session.ensure_fresh()
            except Exception: pass
        frame = self.session.snapshot(1216, quality=85)
        lines = self.ocr.read(frame['jpeg'], min_confidence)
        values = self.recent_secrets() if redact else []
        for value in values:
            for i in vision.secret_lines(lines, value):
                lines[i] = dict(lines[i], text='[redacted]', words=[])
        return lines

    recent_secrets = staticmethod(lambda: [])

    def execute(self, step):
        return getattr(self, 'op_' + step['do'])(step)

    def whatsapp_variants(self):
        return self.session.run_async(apps_svc.installed_variants(self.session.ensure_udid(), 'whatsapp'))

    def _whatsapp_variant(self, s):
        """Personal or Business. Never picks between two installed apps itself."""
        variants = self.whatsapp_variants()
        if not variants: raise ValueError('WhatsApp is not installed')
        choice = str(s.get('app', 'auto')).strip().lower()
        if choice in ('personal', 'business'):
            picked = [v for v in variants if v['label'].lower() == choice]
            if not picked: raise ValueError('WhatsApp %s is not installed' % choice.capitalize())
            return picked[0]
        if len(variants) == 1: return variants[0]
        raise NeedsChoice('Two WhatsApp apps are installed. Ask the human whether to use Personal or Business, '
                          'then run again with app=personal or app=business.', [v['label'].lower() for v in variants])

    def op_whatsapp_open(self, s):
        """Open a chat with prefilled text in the chosen WhatsApp."""
        variant = self._whatsapp_variant(s)
        url = '%s://send?phone=%s&text=%s' % (variant['scheme'], s['phone'], s['text'])
        return apps_svc.launch(self.session.ensure_udid(), variant['bundle_id'], url=url) + ' (%s)' % variant['label']

    def op_whatsapp_launch(self, s):
        """Start the chosen WhatsApp fresh (so it opens on the chat list), with no chat or text."""
        variant = self._whatsapp_variant(s)
        return apps_svc.launch(self.session.ensure_udid(), variant['bundle_id'], restart=True) + ' (%s)' % variant['label']

    def op_open_url(self, s):
        if self.platform == 'android': return android_svc.open_url(self.session.ensure_udid(), s['url'], s.get('bundle_id'))
        return apps_svc.open_url(self.session.ensure_udid(), s['url'], s.get('bundle_id'))

    def op_launch_app(self, s):
        udid = self.session.ensure_udid()
        if self.platform == 'android': return android_svc.launch(udid, s.get('bundle_id') or s['name'])   # package id
        if s.get('bundle_id'): return apps_svc.launch(udid, s['bundle_id'], restart=bool(s.get('restart')))
        if self._apps is None:
            self._apps = self.session.run_async(apps_svc.list_apps(udid, 'Any'))
        want = vision.norm(s['name'])
        exact = [a for a in self._apps if vision.norm(a['name'] or '') == want]
        loose = [a for a in self._apps if want and want in vision.norm(a['name'] or '')]
        hits = exact or loose
        if len(hits) > 1:  # prefer the app the user can actually see on the Home Screen or dock
            state = self.session.run_async(layout_svc.get_layout(udid))
            visible = {i.get('displayIdentifier') for i in state[0] + layout_svc.all_icons(state)}
            hits = [a for a in hits if a['bundle_id'] in visible] or hits
        if len(hits) != 1:
            raise ValueError('No unique app named %r (%s)' % (s['name'], ', '.join(a['name'] for a in (hits or self._apps)[:8])))
        return apps_svc.launch(udid, hits[0]['bundle_id'], restart=bool(s.get('restart')))

    HEADINGS = ('chats', 'contacts', 'groups', 'invite to whatsapp', 'media', 'messages', 'links', 'documents', 'photos')
    BLOCK_GAP = 0.033        # lines closer than this (as a fraction of the screen) belong to the same chat row

    def _search_rows(self):
        """Names in WhatsApp's search results: [{text, base, self, section, y}] for the Chats and Contacts sections.
        A chat row is a name line followed by message-preview lines, so the first line of each block is the name."""
        try: self.session.wait_settled()                          # results load in stages: let the list stop moving
        except Exception: pass
        lines = sorted((l for l in self.lines() if 0.12 < l['box'][1] + l['box'][3] / 2 < 0.9), key=lambda l: l['box'][1])
        cy = lambda l: l['box'][1] + l['box'][3] / 2
        rows, section, prev_y, new_block = [], None, -1.0, True
        for l in lines:
            head = vision.norm(l['text'])
            if head in self.HEADINGS and l['box'][0] < 0.2:
                section, new_block = head, True
                continue
            if section not in ('chats', 'contacts') or l['box'][0] > 0.4: continue
            if cy(l) - prev_y > self.BLOCK_GAP: new_block = True
            prev_y = cy(l)
            if not new_block: continue                             # a message-preview line
            new_block = False
            you = bool(re.search(r'\(\s*you\s*\)\s*$', l['text'], re.I))
            base = re.sub(r'\s*\(\s*you\s*\)\s*$', '', l['text'], flags=re.I).strip()
            if len(vision.norm(base)) >= 2: rows.append(dict(text=l['text'].strip(), base=base, self=you, section=section, y=cy(l)))
        return rows

    def _type_query(self, query):
        self.session.act('press_key', key='cmd+a'); self.session.act('press_key', key='backspace')
        if query.isascii(): self.session.act('type_text', text=query)
        else: self.op_paste(dict(text=query))
        time.sleep(0.5)

    @staticmethod
    def _queries(contact):
        """The full name first; if WhatsApp finds nothing (a typo), shorter beginnings of the longest words."""
        tokens = sorted(vision.norm(contact).split(), key=len, reverse=True)
        out = [contact] + [t[:4] for t in tokens[:2] if len(t) > 4] + [t[:3] for t in tokens[:2] if len(t) > 3]
        return list(dict.fromkeys(out))

    def op_find_chat(self, s):
        """Search WhatsApp for `contact` and open the right chat. One exact match opens it; anything else (two chats with
        the same name, your own chat plus another, a typo with only look-alikes) stops and goes back to the human with the
        names WhatsApp actually shows. `pick` is the human's choice, exactly as listed."""
        contact, pick = s['contact'].strip(), str(s.get('pick', '')).strip()
        want, seen = vision.norm(contact), []
        for query in self._queries(contact):
            self._type_query(query)
            rows = self._search_rows()
            for r in rows:
                if r['text'].casefold() not in [x['text'].casefold() for x in seen]: seen.append(dict(r, query=query))
            if pick:
                hit = [r for r in rows if r['text'].casefold() == pick.casefold()]
                if hit:
                    self.last_chat = hit[0]['base']; self.session.act('tap', x=0.35, y=hit[0]['y'])
                    return 'Opened %r (your choice)' % hit[0]['text']
                continue
            exact = [r for r in rows if vision.norm(r['base']) == want]
            chats = [r for r in exact if r['section'] == 'chats']
            if len(chats) == 1 or (not chats and len(exact) == 1):
                r = (chats or exact)[0]
                self.last_chat = r['base']; self.session.act('tap', x=0.35, y=r['y']); return 'Opened %r' % r['text']
            if len(chats) > 1 or len(exact) > 1:
                labels = [r['text'] + ('' if r['section'] == 'chats' else ' (contact, no chat yet)') for r in (chats or exact)]
                raise NeedsChoice('WhatsApp shows %d entries named %r. Ask the human which one, then run again with pick set '
                                  'to their choice exactly as listed.' % (len(labels), contact), labels)
            if rows and query == contact:
                continue                                           # only look-alikes for the full name: try shorter queries too
        if pick: raise ValueError('The chosen entry %r was not found in WhatsApp search' % pick)
        if not seen: raise ValueError('WhatsApp finds nothing like %r. Ask the human to check the spelling or give the number.' % contact)
        raise NeedsChoice('No chat or contact is named exactly %r. WhatsApp shows these look-alikes: ask the human which one '
                          '(or none), then run again with pick set to their choice exactly as listed.' % contact,
                          [r['text'] for r in seen[:8]])

    last_chat = ''

    def op_assert_header(self, s):
        """The open chat's header must show the name of the entry we tapped (not the words typed in the search box)."""
        if not self.last_chat: raise ValueError('No chat was opened by find_chat')
        if not vision.find(self.lines(), self.last_chat, max_y=float(s.get('max_y', 0.2))):
            raise ValueError('The chat header does not show %r' % self.last_chat)
        return 'Header shows %r' % self.last_chat

    def op_wait_absent(self, s):
        end = time.monotonic() + float(s.get('timeout', 6))
        while True:
            if not self._present(s, self.lines()): return 'Gone'
            if time.monotonic() >= end: raise ValueError('Still on screen after %ss: %r' % (s.get('timeout', 6), s.get('text') or s.get('any')))
            time.sleep(.4)

    def op_tap_xy(self, s):
        return self.session.act('tap', x=float(s['x']), y=float(s['y']), hold=float(s.get('hold', 0.05)))

    def op_tap_text(self, s):
        end, n = time.monotonic() + float(s.get('timeout', 0)), int(s.get('occurrence', 1))
        while True:
            matches = vision.find(self.lines(), s['text'], exact=bool(s.get('exact')), **self._band(s))
            if len(matches) >= n:
                m = matches[n - 1]
                self.session.act('tap', x=m['x'], y=m['y'])
                return 'Tapped %r at (%.3f, %.3f)' % (m['text'], m['x'], m['y'])
            if time.monotonic() >= end: raise ValueError('Text %r not found on screen' % s['text'])
            time.sleep(.5)

    @staticmethod
    def _band(s):
        """Optional min_y / max_y: look only in that band of the screen (for example below a search field)."""
        return dict(min_y=float(s.get('min_y', 0.0)), max_y=float(s.get('max_y', 1.0)))

    def op_type_text(self, s): return self.session.act('type_text', text=s['text'])

    def op_input_text(self, s):
        """Names and short ASCII go in by keyboard; anything else is pasted (exact, any script)."""
        if s['text'].isascii() and len(s['text']) <= 120: return self.op_type_text(s)
        return self.op_paste(s)

    def op_tap_beside_text(self, s):
        """Tap at a fixed x on the row of the draft text (the Send button sits beside the draft, wherever the keyboard
        has pushed it). The lowest on-screen line that is mostly made of the draft's words is the draft's last line."""
        words = set(vision.norm(s['text']).split())
        lines = [l for l in self.lines() if l['box'][1] > float(s.get('min_y', 0.3))]

        def mostly_draft(l):
            own = vision.norm(l['text']).split()
            return len(vision.norm(l['text'])) >= 3 and own and sum(1 for w in own if w in words) / len(own) >= .8
        mine = [l for l in lines if mostly_draft(l)]
        if not mine: raise ValueError('Could not see the draft text on screen, so the Send button was not tapped')
        y = max(l['box'][1] + l['box'][3] / 2 for l in mine)
        self.session.act('tap', x=float(s.get('x', .93)), y=y)
        return 'Tapped beside the draft at (%.2f, %.3f)' % (float(s.get('x', .93)), y)

    def op_paste(self, s):
        if self.platform == 'android': raise ValueError('paste is not available on Android yet')
        udid = self.session.ensure_udid()
        clip_svc.set_text(udid, s['text'])
        return self.session.act('press_key', key='cmd+v')

    def op_press_key(self, s): return self.session.act('press_key', key=s['key'])
    def op_press_button(self, s): return self.session.act('press_button', name=s['name'])

    def _present(self, s, lines):
        band = self._band(s)
        if 'digits' in s: return vision.digits_on_screen(lines, s['digits'])
        if 'any' in s: return any(vision.find(lines, t, exact=bool(s.get('exact')), **band) for t in s['any'])
        if s.get('fuzzy'):
            inside = [l for l in lines if band['min_y'] <= l['box'][1] + l['box'][3] / 2 <= band['max_y']]
            return vision.overlap(inside, s['text']) >= float(s.get('ratio', .6))
        return bool(vision.find(lines, s['text'], exact=bool(s.get('exact')), **band))

    def op_wait_text(self, s):
        end = time.monotonic() + float(s.get('timeout', 8))
        while True:
            if self._present(s, self.lines()): return 'Found'
            if time.monotonic() >= end: raise ValueError('Did not see %r within %ss' % (s.get('text') or s.get('any'), s.get('timeout', 8)))
            time.sleep(.4)

    def op_assert_text(self, s):
        seen = self._present(s, self.lines())
        if s.get('absent'):
            if seen: raise ValueError('Unexpected text on screen: %r' % (s.get('text') or s.get('any')))
            return 'Absent as expected'
        if not seen: raise ValueError('Expected text not on screen: %r' % (s.get('text') or s.get('any')))
        return 'Present'

    def op_wait_change(self, s):
        return 'Changed' if self.session.wait_change(float(s.get('timeout', 5))) else 'No change'

    def op_sleep(self, s):
        time.sleep(min(float(s.get('seconds', 1)), 5)); return 'Slept'


def run(skill, params, approved, ops, approver=None):
    declared = skill.get('params', {})
    unknown = set(params) - set(declared)
    if unknown: raise ValueError('Unknown parameters: ' + ', '.join(sorted(unknown)))
    full = dict(params)
    for key, spec in declared.items():
        if key not in full:
            if isinstance(spec, dict) and 'default' in spec: full[key] = spec['default']
            else: raise ValueError('Missing parameter: ' + key)
    started, done = time.monotonic(), []
    for i, raw in enumerate(skill['steps'], 1):
        step = fill(raw, full)
        if 'when' in step and not str(step['when']).strip():
            done.append(dict(n=i, do=step['do'], ms=0, note='skipped (not needed)'))
            continue
        if step.get('outward') and not approved and approver is not None:
            if approver(skill, full, step) is not True:
                return dict(status='denied', failed_step=i, completed=done,
                            message='The human did not allow this step, so nothing was sent. Do not retry.')
        elif step.get('outward') and not approved:
            return dict(status='awaiting_approval', next_step=i, will_do=step, completed=done,
                        message='Stopped before an outward step. Show the human exactly what will happen, then re-run '
                                'with approved=true only if they agreed (or already gave this exact recipient and content).')
        if time.monotonic() - started > MAX_SECONDS: return dict(status='failed', error='Skill timed out', completed=done)
        t = time.monotonic()
        try:
            note = ops.execute(step)
        except NeedsChoice as choice:
            return dict(status='needs_choice', question=choice.question, options=choice.options, completed=done)
        except Exception as error:
            if step.get('optional'):
                done.append(dict(n=i, do=step['do'], ms=int((time.monotonic() - t) * 1000), note='skipped: ' + str(error)[:80]))
                continue
            return dict(status='failed', failed_step=i, do=step['do'], error=str(error) or type(error).__name__, completed=done)
        done.append(dict(n=i, do=step['do'], ms=int((time.monotonic() - t) * 1000), note=str(note)[:120]))
    return dict(status='ok', seconds=round(time.monotonic() - started, 1), completed=done)
