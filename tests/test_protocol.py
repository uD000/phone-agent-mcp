"""Starts the real MCP server over stdio and checks the protocol and tool list. No phone needed.
Run:  python tests/test_protocol.py"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
env = dict(os.environ, PHONE_AGENT_HOME=tempfile.mkdtemp(prefix='pa-test-'), PHONE_AGENT_CONSOLE='0',
           PYTHONPATH=os.path.join(ROOT, 'src'))
server = subprocess.Popen([sys.executable, '-m', 'phoneagent.mcp_server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, env=env)


def rpc(method, params=None, id_=None):
    message = {'jsonrpc': '2.0', 'method': method}
    if params is not None: message['params'] = params
    if id_ is not None: message['id'] = id_
    server.stdin.write(json.dumps(message) + '\n'); server.stdin.flush()
    return json.loads(server.stdout.readline()) if id_ is not None else None


def check(ok, label):
    print(('  pass  ' if ok else '  FAIL  ') + label)
    if not ok: check.failed = True


check.failed = False
init = rpc('initialize', dict(protocolVersion='2025-06-18', capabilities={}, clientInfo=dict(name='test', version='0')), 1)
check(init['result']['serverInfo']['name'] == 'phone-agent', 'initialize')
rpc('notifications/initialized')
tools = {t['name']: t for t in rpc('tools/list', {}, 2)['result']['tools']}
check(len(tools) >= 40, '%d tools listed' % len(tools))
for name in ('screenshot', 'tap', 'swipe', 'type_text', 'run_skill', 'request_secret', 'type_secret', 'get_policy', 'stop',
             'network_traffic', 'sort_layout', 'list_devices'):
    check(name in tools, 'tool present: ' + name)
check(not any('scan_contacts' in n for n in tools), 'no contact-scan tools')
check('device' in tools['screenshot']['inputSchema']['properties'], 'device-scoped tools take a `device` argument')
check('device' not in tools['get_policy']['inputSchema']['properties'], 'global tools do not')
policy_reply = rpc('tools/call', dict(name='get_policy', arguments={}), 3)['result']
check(json.loads(policy_reply['content'][0]['text'])['approval'] == 'dialog', 'default approval mode is the human dialog')
tap = rpc('tools/call', dict(name='tap', arguments=dict(x=.5, y=.5)), 4)['result']
check(tap['content'][0]['text'].startswith('ERROR'), 'acting with no phone answers with a plain error, not a crash')
server.terminate()
sys.exit(1 if check.failed else 0)
