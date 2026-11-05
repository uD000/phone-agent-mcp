"""Phone session: the single owner of one iPhone's tunnel, screen stream and HID input.

Structure adapted from open-iphone-use (Apache-2.0), see NOTICE. Uses Xcode's CoreDevice
services through pymobiledevice3: an RSD tunnel (USB userspace tunnel, or the tunnel macOS's
`remoted` keeps for a wirelessly paired phone), the display HEVC stream (which must stay up
because it authorizes input) and the universal HID service for touch and keys.

Frame rules: every observation of the screen gets an increasing frame id. After any input the
server waits for a frame observed after the input ended; ids older than that are stale.
"""
import asyncio
import collections
import contextlib
import functools
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import CancelledError

from .keys import chord, fraction, stroke
from .services.common import HOME, private_dir

UNIT = 65535
HID_SERVICE = 'com.apple.coredevice.hid.universalhidservice'
DISPLAY_SERVICE = 'com.apple.coredevice.displayservice'
BUTTONS = dict(home=(0x0C, 0x40), lock=(0x0C, 0x30), volume_up=(0x0C, 0xE9), volume_down=(0x0C, 0xEA),
               mute=(0x0C, 0xE2), siri=(0x0C, 0xCF))
def _settings():
    try: return json.loads((HOME / 'settings.json').read_text())
    except (OSError, ValueError): return {}


# Wi-Fi video quality. Measured on an iPhone 16 Pro: 2, 4 and 6 Mbit/s all give about 56 fps while the screen moves, so a
# higher cap does not make it smoother; it makes text sharper (better OCR) at the cost of more radio traffic and heat.
WIFI_MAX_BITRATE = int(float(os.environ.get('PHONE_AGENT_WIFI_MBIT') or _settings().get('wifi_mbit', 4)) * 1_000_000)


def set_wifi_mbit(mbit):
    """Change the Wi-Fi video cap (1-12 Mbit/s) for new connections and remember it."""
    global WIFI_MAX_BITRATE
    mbit = max(1.0, min(float(mbit), 12.0))
    WIFI_MAX_BITRATE = int(mbit * 1_000_000)
    data = dict(_settings(), wifi_mbit=mbit)
    private_dir(HOME)
    (HOME / 'settings.json').write_text(json.dumps(data))
    (HOME / 'settings.json').chmod(0o600)
    return mbit
HEALTH_EVERY = 10.0            # how often to check the phone is still reachable
HEALTH_FAILS = 3               # consecutive failed checks before reconnecting
TICK = 0.5                     # a still screen sends no video; re-observe it this often
LATENCY = 0.15                 # frames still in the encoder when an input ends may predate its effect
MISSING = 'pymobiledevice3 is not installed in this environment'
_usb_tunnel = threading.Lock()  # pymobiledevice3's userspace stack is process-global: one USB phone


def units(value):
    return round(fraction(value) * UNIT)


def held(modifier, usage):
    return tuple(0xE0 + bit for bit in range(8) if modifier >> bit & 1) + ((usage,) if usage else ())


def to_jpeg(bgra, size, width=None, region=None, quality=70):
    from PIL import Image
    image = Image.frombuffer('RGB', size, bgra, 'raw', 'BGRX', 0, 1)
    with image:
        if region:
            x0, y0, x1, y1 = region
            image = image.crop((round(x0 * size[0]), round(y0 * size[1]), round(x1 * size[0]), round(y1 * size[1])))
        if width and width < image.width:
            image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
        out = io.BytesIO()
        image.convert('RGB').save(out, format='JPEG', quality=quality)
        return out.getvalue(), image.size


def candidates():
    """Paired physical phones reachable now, over USB or Wi-Fi (Xcode's devicectl)."""
    with tempfile.NamedTemporaryFile(suffix='.json') as output:
        subprocess.run(['xcrun', 'devicectl', 'list', 'devices', '--quiet', '--json-output', output.name],
                       check=True, capture_output=True, timeout=20)
        devices = json.load(open(output.name))['result']['devices']
    kinds = dict(wired='USB', localNetwork='Wi-Fi')
    return [dict(udid=d['hardwareProperties']['udid'], name=d['deviceProperties'].get('name'),
                 ios=d['deviceProperties'].get('osVersionNumber'), model=d['hardwareProperties'].get('productType'),
                 connection=kinds.get(d['connectionProperties'].get('transportType'), '?'))
            for d in devices
            if d.get('hardwareProperties', {}).get('reality') == 'physical'
            and d.get('connectionProperties', {}).get('pairingState') == 'paired']


@functools.cache
def stream_connection():
    from pymobiledevice3.remote.core_device.display_service import DisplayService, require_video_stream_support
    from pymobiledevice3.remote.core_device.hid_service import TOUCH_AUTH_SETTLE_SECONDS, TOUCHSCREEN_STATE_RELEASE
    from pymobiledevice3.remote.core_device.screen_stream import open_media_receiver
    from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer

    class StreamConnection(VncStreamServer):
        on_frame = None

        def __init__(self, rsd):
            super().__init__(rsd)
            self.tasks = []
            self.service = self.transport = None
            self.max_bitrate = None

        @property
        def frame(self):
            return self._latest_frame, (self._fb_width, self._fb_height)

        def _broadcast_frame(self, bgra):
            super()._broadcast_frame(bgra)
            if self.on_frame: self.on_frame()

        def request_refresh(self):
            """Ask the phone for a fresh keyframe and rebuild the decoder (used when the picture looks frozen)."""
            def fire():
                self._refresh_pending = False
                self._fire_decoder_refresh(time.monotonic(), reason='stale picture')
            self._loop.call_soon_threadsafe(fire)

        async def open(self):
            require_video_stream_support(self._rsd)
            self.service = DisplayService(self._rsd)
            await self.service.connect()
            self.transport, receiver = self.media_receiver()
            if self._rsd.is_in_process_tunnel:
                start = self.service.start_video_stream(
                    receiver_ip=receiver, receiver_port=self.transport.port, sender_ip=self._sender_ip,
                    display_id=self._display_id, client_session_id=uuid.uuid4())
            else:
                start = self.start_capped_stream(receiver, WIFI_MAX_BITRATE)
            answer = await asyncio.wait_for(start, 15)
            config = answer['connection'].get('streamConfig', {})
            self.max_bitrate = config.get('TXMaxBitrate')
            port = int(config.get('SourcePort', 0))
            # streamConfig names SSRCs from the device's side: ours is RemoteSSRC.
            self._local_ssrc, self._remote_ssrc = int(config.get('RemoteSSRC', 0)), int(config.get('LocalSSRC', 0))
            self._rtcp_dest = (self._sender_ip, port) if port else None
            self._active_transport = self.transport
            self._loop = asyncio.get_running_loop()
            self.tasks = [asyncio.create_task(self._udp_recv_and_pipe(self.transport)),
                          asyncio.create_task(self._decoder_refresh_loop()),
                          asyncio.create_task(self._rtcp_send_loop(self.transport))]
            await asyncio.sleep(TOUCH_AUTH_SETTLE_SECONDS)

        async def start_capped_stream(self, receiver, max_bitrate):
            from pymobiledevice3.remote.core_device import display_service as ds, media_stream_offer as offers
            from pymobiledevice3.remote.xpc_message import XpcInt64Type, XpcUInt64Type
            import plistlib, random, zlib
            tiers = tuple((kind, min(bps, max_bitrate) if kind == 0 else bps, buf)
                          for kind, bps, buf in offers._DEFAULT_VIDEO_BITRATE_TIERS)
            blob = offers.build_media_blob_video(random.randint(0, 0xFFFFFFFF), bitrate_tiers=tiers)
            offer = plistlib.dumps({
                'avcMediaStreamOptionRemoteEndpointInfo': offers.build_remote_endpoint_info('Mac15,9', '2205.3.1', '25F80'),
                'avcMediaStreamNegotiatorMode': offers.NEGOTIATOR_MODE_VIDEO,
                'avcMediaStreamNegotiatorMediaBlob': zlib.compress(blob, level=9),
                'avcMediaStreamOptionCallID': str(uuid.uuid4()).upper()}, fmt=plistlib.FMT_BINARY)
            return await self.service.invoke('com.apple.coredevice.feature.startmediastream', {
                'clientSupportedFeatures': XpcUInt64Type(ds._CLIENT_SUPPORTED_FEATURES), 'direction': 'output',
                'negotiatorOffer': offer,
                'options': {
                    'AVCMediaStreamNegotiatorAccessNetworkType': {'int': XpcInt64Type(ds._DEFAULT_ACCESS_NETWORK_TYPE)},
                    'AVCMediaStreamNegotiatorTransportProtocolType': {'int': XpcInt64Type(ds._DEFAULT_TRANSPORT_PROTOCOL_TYPE)},
                    'CoreDeviceVideoDisplayMode': {'string': 'DisplayByID'},
                    'VideoStreamForDisplayID': {'int': XpcInt64Type(self._display_id)},
                    'avcMediaStreamOptionClientSessionID': {'uuid': uuid.uuid4()}},
                'receiverIP': receiver, 'receiverPort': XpcUInt64Type(self.transport.port), 'senderIP': self._sender_ip,
                'timeout': XpcUInt64Type(20), 'type': 'video'},
                action_identifier='com.apple.coredevice.action.mediastreamstart')

        def media_receiver(self):
            if self._rsd.is_in_process_tunnel:
                return open_media_receiver(self.service, (8 * 1024 * 1024, 4 * 1024 * 1024))
            import socket
            from pymobiledevice3.remote.core_device.screen_stream import _KernelUdp
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.bind(('::', 0))
            for size in (8 * 1024 * 1024, 4 * 1024 * 1024):
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
                    break
            return _KernelUdp(sock), self.service.service.local_address[0]

        async def tap(self, x, y, hold=0.05):
            await self._ensure_hid()
            await self._uhs.multi_tap([(x, y)], hold=hold)

        async def drag(self, x1, y1, x2, y2, duration):
            await self._ensure_hid()
            await self._uhs.multi_drag([(x1, y1, x2, y2)], steps=max(8, round(duration * 60)), duration=duration)

        async def release(self, x, y):
            if self._uhs is None: return
            await self._uhs.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x, y)
            if self._kb_service_id is not None: await self._uhs.send_keyboard(self._kb_service_id, ())

        async def button(self, page, code):
            from pymobiledevice3.remote.core_device.hid_service import HID_BUTTON_STATE_DOWN, HID_BUTTON_STATE_UP
            await self._ensure_indigo()
            await self._indigo.send_button(page, code, HID_BUTTON_STATE_DOWN); await asyncio.sleep(.08)
            await self._indigo.send_button(page, code, HID_BUTTON_STATE_UP)

        async def press(self, keys):
            await self._ensure_keyboard()
            service, keyboard = self._uhs, self._kb_service_id
            modifiers = tuple(u for u in keys if u >= 0xE0)
            if modifiers and len(modifiers) < len(keys):
                await service.send_keyboard(keyboard, modifiers); await asyncio.sleep(.04)
            await service.send_keyboard(keyboard, keys); await asyncio.sleep(.04)
            await service.send_keyboard(keyboard, ()); await asyncio.sleep(.02)

        async def aclose(self):
            for task in self.tasks: task.cancel()
            for task in self.tasks:
                with contextlib.suppress(BaseException): await task
            await self._stop_hid()
            if self._transcoder is not None:
                with contextlib.suppress(Exception): self._transcoder.close()
            if self.transport is not None:
                with contextlib.suppress(Exception): self.transport.close()
            if self.service is not None:
                with contextlib.suppress(Exception): await self.service.close()
                with contextlib.suppress(Exception): await asyncio.wait_for(DisplayService.stop_all_streams(self._rsd), 6)

    return StreamConnection


@contextlib.asynccontextmanager
async def tunnel(udid, transport='auto'):
    """USB: our own rootless userspace tunnel. Wi-Fi: the tunnel macOS's remoted keeps to a paired phone."""
    try:
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        from pymobiledevice3.usbmux import list_devices
        from pymobiledevice3.remote.native_tunnel import NativeRemotedTunnel
    except ImportError:
        raise OSError(MISSING) from None
    on_usb = any(d.serial == udid and d.is_usb for d in await list_devices())
    if transport == 'usb' and not on_usb: raise OSError('The phone is not connected by USB')
    # pymobiledevice3's USB tunnel is process-wide: one phone per process may use it. In auto mode a second USB phone
    # simply goes over Wi-Fi (it needs wireless pairing); an explicit usb request waits briefly, then fails.
    use_usb = transport in ('auto', 'usb') and on_usb and not (transport == 'auto' and _usb_tunnel.locked())
    if use_usb:
        for _ in range(50):                      # wait up to 5 s without blocking the event loop
            if _usb_tunnel.acquire(blocking=False): break
            await asyncio.sleep(0.1)
        else:
            raise OSError('Another USB phone is already in use (connect this one over Wi-Fi instead)')
        try:
            async with UserspaceRsdTunnel(serial=udid) as rsd:
                yield rsd, 'USB'
        finally:
            _usb_tunnel.release()
        return
    native = NativeRemotedTunnel(serial=udid)
    rsd = await native.aopen()
    try:
        yield rsd, 'Wi-Fi'
    finally:
        await native.aclose()


@contextlib.asynccontextmanager
async def open_connection(udid, transport='auto'):
    async with tunnel(udid, transport) as (rsd, kind):
        services = rsd.peer_info.get('Services', {})
        if HID_SERVICE not in services or DISPLAY_SERVICE not in services:
            raise OSError('This iPhone does not publish CoreDevice display and HID services (needs iOS 27 and '
                          'Xcode 27 opened once with the phone connected; Developer Mode on)')
        stream = stream_connection()(rsd)
        try:
            await stream.open()
        except Exception as error:
            await stream.aclose()
            raise OSError('The screen stream that authorizes input did not start: '
                          + (str(error) or type(error).__name__)) from error
        stream.kind = kind
        try:
            yield stream
        finally:
            await stream.aclose()


class PhoneSession:
    """One phone. Runs the connection in a background event loop and exposes a synchronous API."""
    RETRY = 2

    def __init__(self):
        self.cond = threading.Condition()
        self.input_lock = threading.Lock()
        self.conn = None
        self.udid = None
        self.transport = 'auto'
        self.error = 'Not connected'
        self.seq = self.min_valid_seq = 0
        self.observed_at = self.last_input_end = 0.0
        self.real_seq, self._real_times = 0, collections.deque(maxlen=400)
        self.real_at = 0.0                  # when the video last delivered a REAL decoded frame (observed_at is also ticked)
        self._verified_at = 0.0
        self.recoveries = 0
        self.point = (.5, .5)
        self.task = self.running = self._atask = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name='phone-session').start()

    def run_async(self, coro, timeout=90):
        """Run a coroutine on the session loop (used by direct services)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def ensure_udid(self):
        """The connected phone's udid, or the only paired phone reachable now."""
        if self.udid: return self.udid
        found = candidates()
        if len(found) != 1:
            raise OSError('Call connect with a udid first (%d phones found)' % len(found))
        self.udid = found[0]['udid']
        return self.udid

    # ---- connection -------------------------------------------------------------------------
    def connect(self, udid, transport='auto', timeout=45):
        self.disconnect()
        self.udid, self.transport, self.error = udid, transport, 'Connecting'
        self.task = asyncio.run_coroutine_threadsafe(self._start(), self.loop)
        with self.cond:
            self.cond.wait_for(lambda: (self.conn is not None and self.conn_frame_ready())
                               or self.error not in ('Connecting', ''), timeout)
        return self.status()

    def conn_frame_ready(self):
        return self.conn is not None and self.conn.frame[0] is not None

    def disconnect(self):
        """Close the connection and WAIT until the tunnel, stream and USB lock are really released."""
        self.task = None

        async def stop():
            task = self._atask
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException): await task
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(25)
        with self.cond:
            self.conn = None
            self.error = 'Not connected'

    async def _start(self):
        self._atask = asyncio.current_task()
        await self._run()

    async def _run(self):
        while True:
            try:
                async with open_connection(self.udid, self.transport) as conn:
                    conn.on_frame = self._real_frame
                    with self.cond:
                        self.conn, self.error = conn, ''
                    self._observe()
                    last_health, fails = time.monotonic(), 0
                    while True:
                        await asyncio.sleep(TICK)
                        if time.monotonic() - self.observed_at >= TICK: self._observe()
                        if time.monotonic() - last_health >= HEALTH_EVERY:
                            last_health = time.monotonic()
                            fails = 0 if await self._healthy() else fails + 1
                            if fails >= HEALTH_FAILS: raise OSError('Connection lost: the phone stopped answering')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with self.cond:
                    self.error = str(error) or type(error).__name__
                    self.cond.notify_all()
            finally:
                with self.cond:
                    self.conn = None
                    self.cond.notify_all()
            await asyncio.sleep(self.RETRY)

    async def _healthy(self):
        """Cheap reachability check (a static screen sends no video, so silence alone proves nothing)."""
        try:
            from .services.common import lockdown
            ld = await asyncio.wait_for(lockdown(self.udid), 5)
            await asyncio.wait_for(ld.get_value(key='DeviceName'), 5)
            return True
        except Exception:
            return False

    def _real_frame(self):
        now = time.monotonic()
        self.real_at = now
        self._real_times.append(now)
        with self.cond: self.real_seq += 1
        self._observe()

    def fps(self, window=2.0):
        """Real decoded video frames per second over the last `window` seconds."""
        now = time.monotonic()
        return sum(1 for t in list(self._real_times) if now - t <= window) / window

    def wait_real_frame(self, after_seq, timeout=0.5):
        """Block until the video delivers a frame newer than after_seq (or the timeout). Returns the current seq."""
        with self.cond:
            self.cond.wait_for(lambda: self.real_seq != after_seq, timeout)
            return self.real_seq

    def snapshot_fast(self, width=420, quality=60):
        """A small JPEG of the latest frame, cheap enough for 60 per second: shrink the 4-channel frame first (an integer
        box reduction), then swap the colour channels on the small picture (about 9 ms instead of 90)."""
        from PIL import Image
        bgra, size = self.connected().frame
        if bgra is None: raise OSError('No frame has arrived yet')
        small = Image.frombuffer('RGBA', size, bgra, 'raw', 'RGBA', 0, 1).reduce(max(1, round(size[0] / width)))
        b, g, r, _ = small.split()
        out = io.BytesIO()
        Image.merge('RGB', (r, g, b)).save(out, format='JPEG', quality=quality)
        return out.getvalue()

    def _observe_unused(self):
        pass

    def _observe(self):
        with self.cond:
            self.seq += 1
            self.observed_at = time.monotonic()
            self.cond.notify_all()

    def status(self):
        conn = self.conn
        return dict(connected=conn is not None, udid=self.udid, connection=getattr(conn, 'kind', None),
                    max_bitrate=getattr(conn, 'max_bitrate', None), video_fps=round(self.fps(), 1), error=self.error or None,
                    frame_age_ms=int((time.monotonic() - self.observed_at) * 1000) if self.observed_at else None,
                    frame_id=self.seq)

    def connected(self):
        if self.conn is None: raise OSError(self.error or 'Not connected: call connect first')
        return self.conn

    # ---- seeing -----------------------------------------------------------------------------
    def snapshot(self, width=640, region=None, quality=70):
        conn = self.connected()
        with self.cond:
            seq, at = self.seq, self.observed_at
        bgra, size = conn.frame
        if bgra is None: raise OSError('No frame has arrived yet')
        data, out = to_jpeg(bgra, size, width, region, quality)
        return dict(jpeg=data, frame_id=seq, age_ms=int((time.monotonic() - at) * 1000),
                    screen_size=list(size), image_size=list(out))

    def wait_change(self, timeout=5.0):
        """Wait until the screen changes from what it is now. Returns whether it changed."""
        baseline = self._fingerprint()
        changed = self.wait_changed(baseline, timeout=timeout)
        if changed: self.wait_settled()
        return changed

    def _fingerprint(self):
        from PIL import Image
        bgra, size = self.connected().frame
        with Image.frombuffer('RGB', size, bgra, 'raw', 'BGRX', 0, 1) as image:
            return image.resize((32, 64), Image.BILINEAR).tobytes()

    def wait_changed(self, baseline, timeout=0.9, interval=.08, threshold=2.0):
        """Wait for the screen to differ from baseline (the input's effect reaching the video)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            current = self._fingerprint()
            if sum(abs(a - b) for a, b in zip(baseline, current)) / len(current) >= threshold: return True
            time.sleep(interval)
        return False

    def wait_settled(self, timeout=1.5, interval=.12, threshold=2.0):
        """Wait until the screen stops changing (animations done). Returns whether it settled."""
        end, previous = time.monotonic() + timeout, None
        while time.monotonic() < end:
            current = self._fingerprint()
            if previous is not None and sum(abs(a - b) for a, b in zip(previous, current)) / len(current) < threshold:
                return True
            previous = current
            time.sleep(interval)
        return False

    # ---- keeping the picture honest ------------------------------------------------------------------------
    async def _reference_png(self):
        from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
        async with ScreenCaptureService(self.conn._rsd) as service:
            return (await service.capture_screenshot())['image']

    def _reference_fingerprint(self):
        import io
        from PIL import Image
        png = self.run_async(self._reference_png(), 20)
        with Image.open(io.BytesIO(png)) as image:
            return image.convert('RGB').resize((32, 64), Image.BILINEAR).tobytes()

    @staticmethod
    def _diff(a, b): return sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)

    def ensure_fresh(self, min_interval=3.0, threshold=8.0):
        """The video can freeze (it did, on Wi-Fi, right when WhatsApp opened a chat) while the frame counter keeps
        ticking. Compare the picture with an independent screenshot; if they disagree, ask for a fresh keyframe, and if
        that does not help rebuild the stream. Returns True if a recovery was needed."""
        now = time.monotonic()
        if now - self._verified_at < min_interval: return False
        self._verified_at = now
        try:
            reference = self._reference_fingerprint()
        except Exception:
            return False                                     # cannot verify: do not make things worse
        if self._diff(reference, self._fingerprint()) < threshold: return False
        self.recoveries += 1
        before = self.real_at
        try: self.connected().request_refresh()
        except Exception: pass
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            time.sleep(0.25)
            if self.real_at > before and self._diff(reference, self._fingerprint()) < threshold: return True
        udid, transport = self.udid, self.transport          # still frozen: rebuild the whole stream
        self.connect(udid, transport)
        return True

    # ---- acting -----------------------------------------------------------------------------
    def check_frame(self, frame_id):
        if self.conn is None: raise OSError(self.error or 'Not connected: call connect first')
        if frame_id is None:
            if time.monotonic() - self.observed_at > 5: raise OSError('The screen is not live (no frame in 5 s)')
        elif frame_id < self.min_valid_seq:
            raise ValueError('Stale frame_id: input has happened since that screenshot. Take a new screenshot.')

    async def _perform(self, conn, op, a):
        if op == 'tap':
            self.point = (fraction(a['x']), fraction(a['y']))
            hold = max(0.03, min(float(a.get('hold', 0.05)), 1.5))
            await conn.tap(units(self.point[0]), units(self.point[1]), hold)
            return 'Touched (%.3f, %.3f)%s' % (self.point + ((' for %.2fs' % hold,) if hold > 0.06 else ('',)))
        if op == 'drag':
            await conn.drag(units(a['x1']), units(a['y1']), units(a['x2']), units(a['y2']), a.get('duration', .3))
            return 'Dragged'
        if op == 'type_text':
            text = a['text']
            if len(text.encode()) > 500: raise ValueError('Text must be at most 500 ASCII bytes (clipboard paste comes later)')
            strokes = [stroke(c) for c in text]
            for modifier, usage in strokes:
                await conn.press(held(modifier, usage))
            return 'Typed %d characters' % len(text)
        if op == 'press_key':
            await conn.press(held(*chord(a['key'])))
            return 'Pressed ' + a['key']
        if op == 'press_button':
            name = a['name'].lower()
            if name not in BUTTONS: raise ValueError('Button must be one of: ' + ', '.join(BUTTONS))
            await conn.button(*BUTTONS[name])
            return 'Pressed the %s button' % name
        raise ValueError('Unknown operation')

    def act(self, op, frame_id=None, settle=0.0, **args):
        """Run one input, then wait for a frame observed after it ended. Returns the message."""
        with self.input_lock:
            self.check_frame(frame_id)
            conn = self.connected()
            baseline = self._fingerprint()
            future = asyncio.run_coroutine_threadsafe(self._perform(conn, op, args), self.loop)
            self.running = future
            try:
                message = future.result(60)
            except CancelledError:
                raise ValueError('Action canceled by stop') from None
            finally:
                self.running = None
            ended = time.monotonic() + settle
            self.last_input_end = ended
            with self.cond:
                if not self.cond.wait_for(lambda: self.observed_at > ended + LATENCY, 5):
                    raise OSError('No frame arrived after the input')
            changed = self.wait_changed(baseline)
            if not changed and self.ensure_fresh(min_interval=0.0):          # 'no change' may just be a frozen picture
                changed = self._diff(baseline, self._fingerprint()) >= 2.0
            self.wait_settled()
            with self.cond:
                self.min_valid_seq = self.seq
            return message + ('' if changed else ' (the screen did not visibly change)')

    def stop(self):
        running = self.running
        if running: running.cancel()
        conn = self.conn
        if conn is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(conn.release(units(self.point[0]), units(self.point[1])), self.loop).result(3)
        return 'Input canceled and released'
