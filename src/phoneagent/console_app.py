"""The human's console (live phone view, action log, kill switch) as a small web app on loopback.

It is built around the module that owns the phone sessions (`hub`), so it can run inside the daemon, or in a thread of the
MCP server itself: the human then sees exactly what the agent is operating. Access: bearer token, a token cookie, or a
single-use login code (so a pane or browser can be opened without ever showing the long-lived token)."""
import hmac
import json
import os
import secrets as pysecrets
import threading
import time

import anyio
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import approval, audit, killswitch, policy
from .console import PAGE
from .services.common import HOME, private_dir
from .session import candidates

PORT = int(os.environ.get('PHONE_AGENT_PORT', '47831'))
TOKEN_PATH = HOME / 'token'
INFO_PATH = HOME / 'console.json'
CODES = {}                      # single-use login codes -> expiry (monotonic)


def token():
    private_dir(HOME)
    if not TOKEN_PATH.exists():
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, pysecrets.token_urlsafe(32).encode())
        os.close(fd)
    return TOKEN_PATH.read_text().strip()


def _cookie(headers, name):
    for part in headers.get(b'cookie', b'').decode().split(';'):
        key, _, value = part.strip().partition('=')
        if key == name: return value
    return ''


def _take_code(code):
    now = time.monotonic()
    for c in [c for c, t in CODES.items() if t < now]: CODES.pop(c, None)
    return CODES.pop(code, None) is not None


class Guard:
    """Loopback only (Host check), bearer token / cookie / single-use code, and CSRF protection for POSTs."""

    def __init__(self, app, tok, port):
        self.app, self.tok, self.hosts = app, tok, (f'127.0.0.1:{port}', f'localhost:{port}')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode()
        if host not in self.hosts: return await Response('Forbidden host', 403)(scope, receive, send)
        bearer = headers.get(b'authorization', b'').decode().removeprefix('Bearer ').strip()
        query = scope.get('query_string', b'').decode()
        if scope['path'] == '/' and ((query.startswith('t=') and hmac.compare_digest(query[2:], self.tok))
                                     or (query.startswith('c=') and _take_code(query[2:]))):
            response = RedirectResponse('/', status_code=303)
            response.set_cookie('pa_token', self.tok, httponly=True, samesite='strict', path='/')
            return await response(scope, receive, send)
        if not (hmac.compare_digest(bearer, self.tok) or hmac.compare_digest(_cookie(headers, 'pa_token'), self.tok)):
            return await Response('Unauthorized: open the console with `phone-agent console`', 401)(scope, receive, send)
        if scope['method'] == 'POST' and scope['path'].startswith('/api/'):
            origin = headers.get(b'origin', b'').decode()
            if headers.get(b'x-pa') != b'1' or (origin and origin != f'http://{host}'):
                return await Response('CSRF check failed', 403)(scope, receive, send)
        await self.app(scope, receive, send)


def make_routes(hub):
    cache = {'at': 0.0, 'rows': []}

    def devices_cached(max_age=6.0):
        if time.monotonic() - cache['at'] > max_age: cache.update(at=time.monotonic(), rows=hub.all_devices())
        return cache['rows']

    dev_of = lambda request: request.query_params.get('device', '')

    async def index(request): return HTMLResponse(PAGE)

    async def status(request):
        def go():
            rows = devices_cached()
            try:
                with hub.using(dev_of(request)) as dev:
                    s = dict(dev.session.status(), device=dev.label(), name=dev.name, platform=dev.platform, busy=dev.busy)
            except (ValueError, OSError) as error:
                s = dict(connected=False, error=str(error), frame_id=0, connection=None, max_bitrate=None, device=None)
            return dict(s, devices=rows, kill_switch_on=killswitch.is_on(), read_only=hub.READONLY, policy=policy.load())
        return JSONResponse(await anyio.to_thread.run_sync(go))

    def grab(request, width):
        with hub.using(dev_of(request)) as dev:
            f = dev.session.snapshot(width, quality=62)
            return f['jpeg'], f['frame_id']

    async def frame(request):
        width = max(120, min(int(request.query_params.get('w', 420)), 1216))
        try: data, _ = await anyio.to_thread.run_sync(lambda: grab(request, width))
        except (OSError, ValueError): return Response(status_code=503)
        return Response(data, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    async def stream(request):
        """Live view: a multipart JPEG stream (shown as video by a plain <img>). For iPhones every frame the phone's video
        delivers is pushed as it arrives (two encoder threads, so it keeps up with ~55 fps video), up to `fps` (default
        60); other devices are polled."""
        import asyncio
        import threading
        width = max(120, min(int(request.query_params.get('w', 420)), 1000))
        gap = 1.0 / max(1.0, min(float(request.query_params.get('fps', 60)), 60.0))
        quality = max(30, min(int(request.query_params.get('q', 60)), 90))
        device = dev_of(request)
        claim, lock = {'seq': -1}, threading.Lock()

        def next_frame():
            with hub.using(device) as dev:
                s = dev.session
                if not hasattr(s, 'wait_real_frame'):                        # Android: no video, poll screenshots
                    time.sleep(0.05)
                    return None, s.snapshot(width, quality=quality)['jpeg']
                new = s.wait_real_frame(claim['seq'], 0.3)
                with lock:
                    if new == claim['seq']: return None, None                # nothing new yet
                    claim['seq'] = new
                return new, s.snapshot_fast(width, quality)

        async def frames():
            results = asyncio.Queue(maxsize=4)
            closed = False

            async def worker():
                while not closed:
                    try:
                        seq, data = await anyio.to_thread.run_sync(next_frame)
                    except (OSError, ValueError):
                        await anyio.sleep(0.8); continue
                    if data: await results.put((seq, data))
            tasks = [asyncio.create_task(worker()) for _ in range(2)]
            top, last, sent = -1, None, 0.0
            try:
                while True:
                    if await request.is_disconnected(): return
                    try:
                        seq, data = await asyncio.wait_for(results.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if last is None: continue
                        seq, data = top, last                                # a still screen: repeat the last picture
                    if seq is not None and seq < top: continue               # an older frame finished late: drop it
                    now = time.monotonic()
                    if now - sent < gap and seq is not None and seq != top + 0 and results.qsize() > 0: continue
                    top = max(top, seq if seq is not None else top)
                    last, sent = data, now
                    yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n' % len(data) + data + b'\r\n'
            finally:
                closed = True
                for t in tasks: t.cancel()
        return StreamingResponse(frames(), media_type='multipart/x-mixed-replace; boundary=frame',
                                 headers={'Cache-Control': 'no-store'})

    async def audit_rows(request): return JSONResponse(audit.tail(int(request.query_params.get('n', 25))))

    async def devices(request):
        cache['at'] = 0.0
        return JSONResponse(await anyio.to_thread.run_sync(devices_cached))

    async def connect(request):
        body = await request.json() if request.headers.get('content-length', '0') != '0' else {}
        result = await anyio.to_thread.run_sync(lambda: hub.connect(body.get('udid') or dev_of(request), body.get('transport', 'auto')))
        cache['at'] = 0.0
        if result.startswith('ERROR'): return JSONResponse(dict(message='Could not connect: ' + result[7:]))
        info = json.loads(result)
        return JSONResponse(dict(message=('Connected: %s over %s' % (info['device'], info['connection'])) if info['connected']
                                 else 'Not connected: %s' % info['error']))

    async def kill(request):
        private_dir(HOME)
        killswitch.FLAG.touch(mode=0o600)
        audit.log('console', {}, True, 'kill switch ON (console)')
        return JSONResponse(dict(message='Stopped: all phone input is refused until you resume.'))

    async def resume(request):
        """Turning the kill switch off needs the human at the Mac: a native dialog the agent cannot click."""
        ok = await anyio.to_thread.run_sync(lambda: approval.ask(
            'Resume phone control?', 'Turn the kill switch OFF so the AI agent can use the phone again?\n\n'
            'Allow only if you started this from the console.', allow='Resume'))
        if ok:
            killswitch.FLAG.unlink(missing_ok=True)
            audit.log('console', {}, True, 'kill switch OFF (human confirmed)')
        return JSONResponse(dict(message='Resumed.' if ok else 'Not resumed (you did not confirm).'))

    async def quality(request):
        """Wi-Fi video quality (Mbit/s cap). Applies to the selected iPhone by reconnecting its stream."""
        from . import session as session_mod
        body = await request.json() if request.headers.get('content-length', '0') != '0' else {}

        def go():
            mbit = session_mod.set_wifi_mbit(body.get('mbit', 4))
            try:
                with hub.using(dev_of(request)) as dev:
                    s = dev.session
                    if getattr(s, 'conn', None) is not None and getattr(s.conn, 'kind', None) == 'Wi-Fi':
                        s.connect(s.udid, s.transport)
            except (ValueError, OSError):
                pass
            return mbit
        mbit = await anyio.to_thread.run_sync(go)
        return JSONResponse(dict(message='Wi-Fi video cap set to %g Mbit/s.' % mbit))

    async def onetime(request):
        code = pysecrets.token_urlsafe(12)
        CODES[code] = time.monotonic() + 90
        return JSONResponse(dict(code=code, seconds=90))

    return [Route('/', index), Route('/api/status', status), Route('/api/frame.jpg', frame), Route('/api/stream.mjpeg', stream),
            Route('/api/audit', audit_rows), Route('/api/devices', devices), Route('/api/connect', connect, methods=['POST']),
            Route('/api/kill', kill, methods=['POST']), Route('/api/resume', resume, methods=['POST']),
            Route('/api/onetime', onetime, methods=['POST']), Route('/api/quality', quality, methods=['POST'])]


def start_thread(hub, ports=8):
    """Serve the console from a background thread of THIS process (the one that owns the phone sessions). Returns the
    port, or None if no port was free. The URL is recorded in ~/.phone-agent/console.json for `phone-agent console`."""
    import socket
    import uvicorn
    for port in range(PORT, PORT + ports):
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0: continue        # something already listens there
        app = Starlette(routes=make_routes(hub))
        app.add_middleware(Guard, tok=token(), port=port)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', lifespan='off'))
        threading.Thread(target=server.run, daemon=True, name='console').start()
        private_dir(HOME)
        INFO_PATH.write_text(json.dumps(dict(port=port, pid=os.getpid())))
        INFO_PATH.chmod(0o600)
        return port
    return None
