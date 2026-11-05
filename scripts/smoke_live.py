"""Live smoke test against your own phone through the real MCP server (stdio). It only LOOKS and presses Home: it never
sends, posts or changes anything else.

    venv/bin/python scripts/smoke_live.py [--device NAME_OR_UDID] [--transport auto|usb|wifi]

Needs: setup.sh done, the phone unlocked, trusted and on the same network (or USB), Developer Mode on."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
parser = argparse.ArgumentParser()
parser.add_argument('--device', default='')
parser.add_argument('--transport', default='auto')
args = parser.parse_args()
env = dict(os.environ, PHONE_AGENT_CONSOLE='0', PYTHONPATH=os.path.join(ROOT, 'src'))
server = subprocess.Popen([sys.executable, '-m', 'phoneagent.mcp_server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, env=env)
_id = [0]


def rpc(method, params=None, notify=False):
    msg = {'jsonrpc': '2.0', 'method': method}
    if params is not None: msg['params'] = params
    if not notify:
        _id[0] += 1; msg['id'] = _id[0]
    server.stdin.write(json.dumps(msg) + '\n'); server.stdin.flush()
    return None if notify else json.loads(server.stdout.readline())


def tool(tool_name, **arguments):
    started = time.time()
    result = rpc('tools/call', dict(name=tool_name, arguments=arguments))['result']
    text = ' '.join(p.get('text', '') for p in result['content'] if p['type'] == 'text')
    images = sum(1 for p in result['content'] if p['type'] == 'image')
    return text, images, time.time() - started, bool(result.get('isError'))


failed = []


def check(label, ok, detail=''):
    print(('  pass  ' if ok else '  FAIL  ') + label + (('  (' + detail + ')') if detail else ''))
    if not ok: failed.append(label)


rpc('initialize', dict(protocolVersion='2025-06-18', capabilities={}, clientInfo=dict(name='smoke', version='0')))
rpc('notifications/initialized', notify=True)
text, _, took, _ = tool('list_devices')
devices = json.loads(text) if text.startswith('[') else []
phones = [d for d in devices if d.get('platform') == 'ios'] if not args.device else \
    [d for d in devices if args.device.casefold() in (d['name'].casefold(), d['udid'].casefold())]
check('list_devices finds a phone', bool(phones), '%d device(s): %s' % (len(devices), ', '.join(d['name'] for d in devices)))
if not phones:
    print('\nRun `venv/bin/phone-agent doctor` to see what is missing.'); sys.exit(1)
name = phones[0]['name']
text, _, took, _ = tool('connect', udid=phones[0]['udid'], transport=args.transport)
info = json.loads(text) if text.startswith('{') else {}
check('connect', bool(info.get('connected')), '%s over %s in %.1fs' % (name, info.get('connection'), took) if info else text[:120])
if not info.get('connected'): sys.exit(1)
text, images, took, _ = tool('screenshot', width=480, device=name)
check('screenshot returns an IMAGE over MCP', images == 1, '%.1fs' % took)
text, _, took, _ = tool('device_info', device=name)
check('device_info', 'ios' in text, text[:90])
text, _, took, _ = tool('read_screen_text', device=name)
check('read_screen_text (OCR helper)', text.startswith('['), 'built with setup.sh' if not text.startswith('[') else '%d texts, %.1fs' % (len(json.loads(text)), took))
text, images, took, _ = tool('press_button', name='home', device=name)
if 'kill switch' in text:
    print('  skip  press_button home  (the kill switch is ON: run bin/pa-resume to clear it; this refusal is correct behavior)')
else:
    check('press_button home', images == 1, '%.1fs' % took)
tool('disconnect', device=name)
server.terminate()
print('\n%s' % ('All live checks passed.' if not failed else 'FAILED: ' + ', '.join(failed)))
sys.exit(1 if failed else 0)
