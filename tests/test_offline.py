"""Offline tests: no phone, no Xcode device needed. Run:  python tests/test_offline.py

They use a throwaway ~/.phone-agent (PHONE_AGENT_HOME) so nothing on your Mac is touched."""
import asyncio
import base64
import os
import sys
import tempfile
import time
import traceback

HOME = tempfile.mkdtemp(prefix='pa-test-')
os.environ['PHONE_AGENT_HOME'] = HOME
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from phoneagent import killswitch, names, policy, vision  # noqa: E402
from phoneagent.secrets import Secrets  # noqa: E402
from phoneagent.skills import engine as E  # noqa: E402

E.time.sleep = lambda s: None          # scripted screens do not need real waiting


def L(*rows):
    """OCR-like lines from (text, x, y) triples."""
    return [dict(text=t, confidence=1, box=[x, y - .012, .4, .024],
                 words=[dict(text=w, box=[x, y, .1, .03]) for w in t.split()]) for t, x, y in rows]


class FakeSession:
    def __init__(self): self.query, self.log = '', []
    def ensure_udid(self): return 'FAKE'
    def wait_settled(self, *a, **k): return True

    def act(self, op, **a):
        if op == 'type_text': self.query = a['text'].casefold()
        if op == 'press_key' and a.get('key') == 'backspace': self.query = ''
        if op == 'tap': self.log.append(('tap', round(a['x'], 2), round(a['y'], 2)))
        return 'ok'


class FakeOps(E.Ops):
    def __init__(self, screens):
        self.session, self.ocr, self._apps, self.screens = FakeSession(), None, None, screens

    def lines(self, c=.4, redact=True): return self.screens.get(self.session.query, L(('Chats', .05, .174)))


# ---- names ------------------------------------------------------------------------------------------------------------
def test_names():
    pool = ['Aarav Mehta', 'Priya Nair', 'Priya Menon']
    assert names.is_exact('aarav-mehta', 'Aarav Mehta')
    assert not names.is_exact('arav meta', 'Aarav Mehta')
    top = names.suggest('arav meta', pool)[0]
    assert top['name'] == 'Aarav Mehta' and not top['exact']
    assert names.canonical('PRIYA nair', pool) == 'Priya Nair'
    for bad in ('priya', 'arav meta', 'nobody'):
        try: names.canonical(bad, pool); raise AssertionError('accepted ' + bad)
        except ValueError: pass


# ---- policy -----------------------------------------------------------------------------------------------------------
def test_policy():
    p = policy.load()
    assert p['approval'] == 'dialog' and p['precheck'] is True
    assert policy.outward_label('Send') == 'send' and policy.outward_label('Sending soon') is None
    p['app_allowlist'] = ['com.example.ok']; p['app_denylist'] = ['com.example.bad']; p['max_inputs_per_minute'] = 3
    policy.save(p)
    assert policy.app_allowed('com.example.ok')[0] and not policy.app_allowed('com.example.other')[0]
    assert not policy.app_allowed('com.example.bad')[0]
    limiter = policy.RateLimiter()
    assert [limiter.hit() for _ in range(5)] == [True, True, True, False, False]
    p['app_allowlist'] = None; p['max_inputs_per_minute'] = 90; p['app_denylist'] = []
    policy.save(p)


# ---- skills -----------------------------------------------------------------------------------------------------------
def test_skills_valid():
    skills = E.load_all()
    assert {'open_app', 'whatsapp_send', 'whatsapp_send_to_contact'} <= set(skills)
    for sk in skills.values(): E.validate(sk)
    for name in ('whatsapp_send', 'whatsapp_send_to_contact'):
        assert sum(1 for s in skills[name]['steps'] if s.get('outward')) == 1, name
    assert E.fill({'u': 'x://?p={phone|digits}&t={text|urlencode}'}, dict(phone='+1 (555) 01', text='a b'))['u'] == 'x://?p=155501&t=a%20b'


def test_approval_gate():
    sk = E.load_all()['whatsapp_send_to_contact']
    for allow in (False, True):
        ran = []

        class Fake:
            def execute(self, step): ran.append(step['do']); return 'ok'
        r = E.run(sk, dict(contact='Alex', text='hi', app='personal'), False, Fake(), lambda *_: allow)
        assert ('tap_beside_text' in ran) == allow, 'the Send step must run only when the human allows'
        assert r['status'] == ('ok' if allow else 'denied')
    # no approver and no approval: the run stops before the outward step (run_skill always passes approved=False plus the
    # human dialog when the policy is 'dialog', so an agent's own approved=true never reaches here)
    r = E.run(sk, dict(contact='Alex', text='hi', app='personal'), False, type('F', (), {'execute': lambda s, st: 'ok'})())
    assert r['status'] == 'awaiting_approval'


# ---- WhatsApp search results (layout of the real app: name line, then preview lines) --------------------------------------
RESULTS = {
    'sam': L(('Sam', .20, .095), ('Chats', .05, .174), ('Sam (You)', .21, .22), ('https://example.com/a', .25, .244), ('more text', .25, .268),
             ('Some Group & Friends', .21, .31), ('you reacted', .25, .334), ('Sam', .21, .40), ('a message', .25, .425), ('another line', .25, .447),
             ('Contacts', .05, .51), ('Sam Carter', .18, .565), ('Messages', .05, .89)),
    'jordan lee': L(('Jordan Lee', .20, .095), ('Chats', .05, .174), ('Jordan Lee', .21, .22), ('hello', .25, .244), ('Contacts', .05, .51)),
    'jordn lee': L(('jordn lee', .20, .095), ('No results found', .3, .4)),
    'jord': L(('jord', .20, .095), ('Chats', .05, .174), ('Jordan Lee', .21, .22), ('hello', .25, .244)),
    'zzzz': L(('zzzz', .20, .095), ('No results found', .3, .4)),
}


def run_find(**step):
    ops = FakeOps(RESULTS)
    try: return ('ok', ops.execute(dict(do='find_chat', **step)), ops.session.log, ops.last_chat)
    except E.NeedsChoice as c: return ('choice', c.options)
    except ValueError as e: return ('error', str(e))


def test_find_chat():
    assert run_find(contact='Sam') == ('choice', ['Sam (You)', 'Sam']), 'your own chat plus another chat with the same name must ask'
    assert run_find(contact='Sam', pick='Sam (You)')[2] == [('tap', 0.35, 0.22)]
    assert run_find(contact='Sam', pick='Sam')[2] == [('tap', 0.35, 0.4)]
    assert run_find(contact='Jordan Lee')[0] == 'ok' and run_find(contact='Jordan Lee')[2] == [('tap', 0.35, 0.22)]
    kind, options = run_find(contact='jordn lee')
    assert kind == 'choice' and options == ['Jordan Lee'], 'a typo must go back to the human with WhatsApp\'s own look-alike'
    assert run_find(contact='jordn lee', pick='Jordan Lee')[0] == 'ok'
    assert run_find(contact='zzzz')[0] == 'error'


def test_tap_beside_text():
    screens = {'': L(('Jordan Lee', .45, .08), ('Hello how are', .2, .52), ('you doing today', .2, .56), ('q w e r t y', .5, .70))}
    ops = FakeOps(screens)
    ops.execute(dict(do='tap_beside_text', text='Hello how are you doing today', x=.93))
    assert ops.session.log == [('tap', 0.93, 0.56)], 'Send is tapped beside the LAST draft line, not on the keyboard'
    try: FakeOps({'': L(('Jordan Lee', .45, .08))}).execute(dict(do='tap_beside_text', text='Hello how are you'))
    except ValueError: pass
    else: raise AssertionError('must refuse when the draft is not visible')


# ---- screen reading, credentials -----------------------------------------------------------------------------------------
def test_vision():
    assert vision.classify_screen(L(('Enter your password', .1, .2), ('Password', .1, .3), ('Google', .1, .1)))['kind'] == 'password'
    assert vision.classify_screen(L(('Settings', .1, .1), ('Wi-Fi', .1, .2)))['kind'] is None
    assert vision.human_only_screen(L(('Enter Passcode', .3, .3))) == 'enter passcode'
    assert vision.secret_visible(L(('hello', .1, .1), ('zq7test9', .1, .2)), 'zq7test9')
    assert vision.secret_visible(L(('zq7tes19', .1, .2)), 'zq7test9'), 'OCR misreads of a typed secret must still count'
    assert not vision.secret_visible(L(('Password', .1, .1), ('........', .1, .2)), 'zq7test9')
    lines = L(('Send', .8, .9), ('Cancel', .1, .9))
    assert vision.label_at(lines, .85, .9) == 'Send' and vision.label_at(lines, .5, .5) is None


def test_secrets_store():
    s = Secrets()
    s._items['s_1'] = dict(label='x', field='password', expect='', value='correct-horse', uses=1, expires=time.monotonic() + 60)
    assert s.peek('s_1')['value'] == 'correct-horse'
    s.use('s_1')
    assert 'correct-horse' in s.recent_values(), 'a just-typed secret is remembered briefly so screenshots can be checked'
    try: s.peek('s_1'); raise AssertionError('secret must be single use')
    except ValueError: pass
    assert s.forget_all() == 0 and s.recent_values() == []


# ---- server pieces ----------------------------------------------------------------------------------------------------------
def test_kill_switch():
    import phoneagent.mcp_server as m
    assert m.mutate_guard() is None
    killswitch.FLAG.parent.mkdir(parents=True, exist_ok=True)
    killswitch.FLAG.touch()
    try: assert 'kill switch' in (m.mutate_guard() or '')
    finally: killswitch.FLAG.unlink()
    assert m.mutate_guard() is None


def test_images_survive_mcp():
    """A tool that returns [Image, text] must work through the real MCP layer (it once failed with a union annotation)."""
    from mcp.server.mcpserver import Image, MCPServer
    server = MCPServer('t')
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')

    @server.tool(structured_output=False)
    def shot() -> list | str: return [Image(data=png, format='png'), 'meta']
    result = asyncio.run(server.call_tool('shot', {}))
    assert [type(c).__name__ for c in result.content] == ['ImageContent', 'TextContent']


if __name__ == '__main__':
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_')]
    failed = 0
    for name, fn in tests:
        try: fn(); print('  pass  ' + name)
        except Exception:
            failed += 1; print('  FAIL  ' + name); traceback.print_exc()
    print('\n%d passed, %d failed' % (len(tests) - failed, failed))
    sys.exit(1 if failed else 0)
