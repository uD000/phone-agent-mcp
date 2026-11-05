"""phone-agent daemon: ONE process owns the phone. Agents connect over loopback HTTP (MCP at /mcp, bearer token); the human
gets the console at / (see console_app.py). Resuming after the kill switch needs a click in a native dialog."""
import os
import webbrowser

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from . import mcp_server as hub
from .console_app import INFO_PATH, PORT, TOKEN_PATH, Guard, make_routes, token


def build_app():
    hosts = [f'127.0.0.1:{PORT}', f'localhost:{PORT}']
    app = hub.mcp.streamable_http_app(
        streamable_http_path='/mcp',
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                     allowed_origins=['http://' + h for h in hosts]))
    app.router.routes.extend(make_routes(hub))
    app.add_middleware(Guard, tok=token(), port=PORT)
    return app


def main():
    import json
    INFO_PATH.write_text(json.dumps(dict(port=PORT, pid=os.getpid())))
    print('phone-agent daemon on http://127.0.0.1:%d  (MCP at /mcp, console at /)  token: %s' % (PORT, TOKEN_PATH))
    uvicorn.run(build_app(), host='127.0.0.1', port=PORT, log_level='warning')


def one_time_url():
    """A URL that logs the browser in once (valid 90 s), so the long-lived token is never put in a URL."""
    import json
    import urllib.request
    port = json.loads(INFO_PATH.read_text())['port'] if INFO_PATH.exists() else PORT
    req = urllib.request.Request('http://127.0.0.1:%d/api/onetime' % port, method='POST',
                                 headers={'Authorization': 'Bearer ' + token(), 'X-PA': '1'})
    with urllib.request.urlopen(req, timeout=5) as r:
        code = json.loads(r.read())['code']
    return 'http://127.0.0.1:%d/?c=%s' % (port, code)


def open_console():
    webbrowser.open(one_time_url())


if __name__ == '__main__':
    main()
