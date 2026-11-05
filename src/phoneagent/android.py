"""Android backend over adb. Same session interface the tools use for iPhones (screenshot, tap, swipe, type, keys),
plus apps, deep links and basic device info. ASCII text only; clipboard, layout sorting and traffic are iOS-only for now."""
import io
import os
import re
import shutil
import struct
import subprocess
import threading
import time

ADB = os.environ.get('PHONE_AGENT_ADB') or shutil.which('adb') or '/opt/homebrew/bin/adb'
TICK_TIMEOUT = 25
KEYS = dict(enter=66, return_=66, tab=61, space=62, backspace=67, delete=112, escape=111, esc=111, up=19, down=20, left=21,
            right=22, home=3, back=4, recents=187, power=26, lock=26, volume_up=24, volume_down=25, mute=164, menu=82,
            search=84, pageup=92, pagedown=93, select=23, center=23, dpad_center=23, ok=23, play_pause=85, play=126, pause=127,
            next=87, previous=88, fast_forward=90, rewind=89, channel_up=166, channel_down=167, settings=176, guide=172,
            info=165, tv_input=178)
BUTTONS = ('home', 'back', 'recents', 'power', 'volume_up', 'volume_down', 'mute', 'lock', 'menu', 'select', 'play_pause',
           'settings', 'guide')
UNSAFE_SCHEMES = ('file', 'content', 'javascript', 'intent', 'data', 'vbscript')
WHATSAPP = [dict(label='Personal', package='com.whatsapp'), dict(label='Business', package='com.whatsapp.w4b')]


def adb(serial, *args, timeout=TICK_TIMEOUT, binary=False):
    cmd = [ADB] + (['-s', serial] if serial else []) + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise OSError('adb is not installed (brew install android-platform-tools)') from None
    except subprocess.TimeoutExpired:
        raise OSError('adb timed out') from None
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).decode('utf-8', 'replace').strip().splitlines()[-1:] and
                      (r.stderr or r.stdout).decode('utf-8', 'replace').strip().splitlines()[-1] or 'adb failed')
    return r.stdout if binary else r.stdout.decode('utf-8', 'replace')


def list_android():
    """Android devices adb can see: serial, name, state, connection (USB or Wi-Fi)."""
    try:
        out = adb(None, 'devices', '-l', timeout=10)
    except OSError:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2: continue
        info = dict(p.split(':', 1) for p in parts[2:] if ':' in p)
        serial = parts[0]
        rows.append(dict(udid=serial, platform='android', name=(info.get('model') or serial).replace('_', ' '),
                         ios=None, model=info.get('device'), state=parts[1],
                         connection='Wi-Fi' if re.match(r'^[\d.]+:\d+$|.*_adb-tls-connect', serial) else 'USB'))
    return rows


def esc_input(text):
    """Escape text for `adb shell input text` (spaces are %s; shell metacharacters need a backslash)."""
    if not text.isascii(): raise ValueError('Typing on Android is ASCII only for now')
    if '%' in text: raise ValueError('The % character cannot be typed through adb input')
    out = []
    for c in text:
        if c == ' ': out.append('%s')
        elif c in '&<>;|()$`\\"\'*?[]{}#~!^': out.append('\\' + c)
        elif c == '\n': raise ValueError('Use press_key return instead of a newline')
        else: out.append(c)
    return ''.join(out)


class _Conn:
    """Stands in for the iPhone connection object the tools peek at."""
    _rsd = None

    def __init__(self, kind): self.kind, self.frame = kind, (True, None)


class AndroidSession:
    platform = 'android'
    RETRY = 2

    def __init__(self):
        self.udid, self.name, self.conn, self.error = None, None, None, 'Not connected'
        self.is_tv = False
        self._cache = (0.0, None)          # (monotonic time, image) of the latest capture
        self.cap_seconds = None            # how long one capture takes on this device
        self.seq = self.min_valid_seq = 0
        self.observed_at = self.last_input_end = 0.0
        self.size = None
        self.input_lock = threading.Lock()
        self.point = (.5, .5)

    # ---- connection ---------------------------------------------------------------------------
    def connect(self, serial, transport='auto', timeout=20):
        state = adb(serial, 'get-state', timeout=timeout).strip()
        if state != 'device': raise OSError('Android device is %s (authorize USB debugging on the phone)' % state)
        self.udid, self.error = serial, ''
        out = adb(serial, 'shell', 'wm', 'size')
        sizes = re.findall(r'(\d+)x(\d+)', out)
        self.size = tuple(map(int, sizes[-1])) if sizes else None     # the override size wins over the physical one
        try: self.is_tv = 'tv' in adb(serial, 'shell', 'getprop', 'ro.build.characteristics').lower()
        except OSError: self.is_tv = False
        self.conn = _Conn('Wi-Fi' if ':' in serial else 'USB')
        self._observe()
        return self.status()

    def disconnect(self):
        self.conn, self.error = None, 'Not connected'

    def _observe(self):
        self.seq += 1
        self.observed_at = time.monotonic()

    def status(self):
        return dict(connected=self.conn is not None, udid=self.udid, connection=getattr(self.conn, 'kind', None),
                    max_bitrate=None, error=self.error or None, platform='android', screen=self.size, tv=self.is_tv,
                    capture_seconds=round(self.cap_seconds, 2) if self.cap_seconds else None,
                    frame_age_ms=int((time.monotonic() - self.observed_at) * 1000) if self.observed_at else None,
                    frame_id=self.seq)

    def connected(self):
        if self.conn is None: raise OSError(self.error or 'Not connected: call connect first')
        return self.conn

    def ensure_udid(self):
        if not self.udid: raise OSError('Call connect first')
        return self.udid

    def run_async(self, *a, **k): raise OSError('This tool is not available on Android')

    # ---- seeing -------------------------------------------------------------------------------
    def _grab(self, max_age=0.0):
        """Capture the screen. Raw capture is much faster than PNG on weak devices (a TV: 1.3 s vs 4 s); some vendors
        print debug lines before the data, so the header is found by its known size instead of assumed at byte 0."""
        from PIL import Image
        if max_age and self._cache[1] is not None and time.monotonic() - self._cache[0] < max_age:
            return self._cache[1]
        serial = self.ensure_udid()
        started = time.monotonic()
        image = None
        if self.size:
            w, h = self.size
            data = adb(serial, 'exec-out', 'screencap', binary=True)
            at = data.find(struct.pack('<III', w, h, 1))
            if at >= 0 and len(data) - at - 12 >= w * h * 4:
                pixels = data[len(data) - w * h * 4:]          # a newer header adds 4 bytes; pixels are always last
                image = Image.frombuffer('RGBA', (w, h), pixels, 'raw', 'RGBA', 0, 1).convert('RGB')
        if image is None:
            data = adb(serial, 'exec-out', 'screencap', '-p', binary=True)
            start = data.find(b'\x89PNG\r\n\x1a\n')
            if start < 0:
                raise OSError('screencap returned no image (%s)' % data[:80].decode('utf-8', 'replace').strip().replace('\n', ' | '))
            image = Image.open(io.BytesIO(data[start:])).convert('RGB')
        took = time.monotonic() - started
        self.cap_seconds = took if self.cap_seconds is None else .6 * self.cap_seconds + .4 * took
        self._observe()
        self.size = image.size
        self._cache = (time.monotonic(), image)
        return image

    def snapshot(self, width=640, region=None, quality=70, max_age=1.5):
        from PIL import Image
        self.connected()
        image = self._grab(max_age)
        full = image.size
        if region:
            x0, y0, x1, y1 = region
            image = image.crop((round(x0 * full[0]), round(y0 * full[1]), round(x1 * full[0]), round(y1 * full[1])))
        if width and width < image.width:
            image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format='JPEG', quality=quality)
        return dict(jpeg=out.getvalue(), frame_id=self.seq, age_ms=0, screen_size=list(full), image_size=list(image.size))

    def _fingerprint(self, max_age=0.0):
        from PIL import Image
        return self._grab(max_age).resize((32, 64), Image.BILINEAR).tobytes()

    @staticmethod
    def _diff_prints(a, b): return sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)

    def wait_changed(self, baseline, timeout=0.9, interval=.2, threshold=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            cur = self._fingerprint()
            if sum(abs(a - b) for a, b in zip(baseline, cur)) / len(cur) >= threshold: return True
            time.sleep(interval)
        return False

    def wait_settled(self, timeout=2.5, interval=.3, threshold=2.0):
        end, prev = time.monotonic() + timeout, None
        while time.monotonic() < end:
            cur = self._fingerprint()
            if prev is not None and sum(abs(a - b) for a, b in zip(prev, cur)) / len(cur) < threshold: return True
            prev = cur
            time.sleep(interval)
        return False

    def wait_change(self, timeout=5.0):
        changed = self.wait_changed(self._fingerprint(), timeout=timeout)
        if changed: self.wait_settled()
        return changed

    # ---- acting -------------------------------------------------------------------------------
    def check_frame(self, frame_id):
        self.connected()
        if frame_id is not None and frame_id < self.min_valid_seq:
            raise ValueError('Stale frame_id: input has happened since that screenshot. Take a new screenshot.')

    def _px(self, x, y):
        if not self.size: self._grab()
        return round(x * self.size[0]), round(y * self.size[1])

    def _perform(self, op, a):
        serial = self.ensure_udid()
        if self.is_tv and op in ('tap', 'drag'):
            raise ValueError('This is an Android TV with no touchscreen. Navigate with press_key (up, down, left, right, select, '
                             'back, home, menu, play_pause) and look at the screenshot to see what has focus.')
        if op == 'tap':
            self.point = (float(a['x']), float(a['y']))
            px, py = self._px(*self.point)
            adb(serial, 'shell', 'input', 'tap', px, py)
            return 'Touched (%.3f, %.3f)' % self.point
        if op == 'drag':
            x1, y1 = self._px(float(a['x1']), float(a['y1']))
            x2, y2 = self._px(float(a['x2']), float(a['y2']))
            adb(serial, 'shell', 'input', 'swipe', x1, y1, x2, y2, int(float(a.get('duration', .3)) * 1000))
            return 'Dragged'
        if op == 'type_text':
            text = a['text']
            if len(text.encode()) > 500: raise ValueError('Text must be at most 500 ASCII bytes')
            adb(serial, 'shell', 'input', 'text', esc_input(text))
            return 'Typed %d characters' % len(text)
        if op == 'press_key':
            key = str(a['key']).lower().replace('enter', 'enter')
            if '+' in key: raise ValueError('Key chords are not supported on Android yet')
            if key == 'return': key = 'return_'
            if key in KEYS: code = KEYS[key]
            elif len(key) == 1 and key.isalnum(): adb(serial, 'shell', 'input', 'text', key); return 'Typed ' + key
            else: raise ValueError('Unknown key %r. Use up, down, left, right, select, back, home, menu, play_pause, return, tab, space, backspace, escape or a character' % key)
            adb(serial, 'shell', 'input', 'keyevent', code)
            return 'Pressed ' + str(a['key'])
        if op == 'press_button':
            name = str(a['name']).lower()
            if name not in BUTTONS: raise ValueError('Button must be one of: ' + ', '.join(BUTTONS))
            adb(serial, 'shell', 'input', 'keyevent', KEYS[name])
            return 'Pressed the %s button' % name
        raise ValueError('Unknown operation')

    def act(self, op, frame_id=None, settle=0.0, **args):
        with self.input_lock:
            self.check_frame(frame_id)
            baseline = self._fingerprint(max_age=6.0)          # a recent frame is a good enough 'before'
            message = self._perform(op, args)
            self.last_input_end = time.monotonic() + settle
            if self.cap_seconds and self.cap_seconds > 1.0:
                # a slow device (a TV needs ~2 s per capture): wait a moment, then ONE capture decides whether it moved
                time.sleep(0.6)
                changed = self._diff_prints(baseline, self._fingerprint()) >= 2.0
            else:
                changed = self.wait_changed(baseline)
                if changed: self.wait_settled()                 # animations only matter if something moved
            self.min_valid_seq = self.seq
            return message + ('' if changed else ' (the screen did not visibly change)')

    def stop(self):
        return 'Input canceled (adb commands are atomic; nothing is held)'


# ---- Android services ----------------------------------------------------------------------------
def list_packages(serial, kind='User'):
    flag = {'User': '-3', 'System': '-s', 'Any': ''}[kind]
    out = adb(serial, 'shell', 'pm', 'list', 'packages', *([flag] if flag else []))
    return sorted((dict(bundle_id=l.split(':', 1)[1].strip(), name=l.split(':', 1)[1].strip(), type=kind)
                   for l in out.splitlines() if l.startswith('package:')), key=lambda r: r['bundle_id'])


def launch(serial, package):
    from . import policy
    if not re.match(r'^[A-Za-z][A-Za-z0-9_.]{2,200}$', package): raise ValueError('Invalid package name')
    allowed, why = policy.app_allowed(package)
    if not allowed: raise ValueError('%s %s' % (package, why))
    out = adb(serial, 'shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER', '1')
    if 'No activities found' in out: raise ValueError('%s has no launcher activity (is it installed?)' % package)
    return 'Launched ' + package


def open_url(serial, url, package=None):
    scheme = url.split(':', 1)[0].lower() if ':' in url else ''
    if not scheme or len(url) > 2000 or scheme in UNSAFE_SCHEMES: raise ValueError('Not an allowed URL')
    args = ['shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d', "'%s'" % url.replace("'", '%27')]
    if package:
        from . import policy
        allowed, why = policy.app_allowed(package)
        if not allowed: raise ValueError('%s %s' % (package, why))
        args += ['-p', package]
    out = adb(serial, *args)
    if 'Error' in out: raise ValueError(out.strip().splitlines()[-1][:160])
    return 'Opened the URL'


def whatsapp_variants(serial):
    installed = {p['bundle_id'] for p in list_packages(serial, 'Any') if p['bundle_id'] in ('com.whatsapp', 'com.whatsapp.w4b')}
    return [v for v in WHATSAPP if v['package'] in installed]


def device_info(serial):
    prop = lambda k: adb(serial, 'shell', 'getprop', k).strip()
    info = dict(name=prop('ro.product.model'), model=prop('ro.product.device'), ios=None, android=prop('ro.build.version.release'),
                sdk=prop('ro.build.version.sdk'))
    battery = battery_status(serial)
    info.update(battery_percent=battery.get('level'), charging=battery.get('charging'))
    return info


def battery_status(serial):
    out = adb(serial, 'shell', 'dumpsys', 'battery')
    kv = dict(re.findall(r'^\s*([\w ]+?):\s*(.+)$', out, re.M))
    num = lambda k: int(kv[k]) if k in kv and kv[k].strip().lstrip('-').isdigit() else None
    temp = num('temperature')
    return dict(level=num('level'), charging=kv.get('AC powered') == 'true' or kv.get('USB powered') == 'true',
                status=kv.get('status'), health=kv.get('health'), temperature_c=round(temp / 10, 1) if temp is not None else None,
                voltage_mv=num('voltage'))


def tail_logs(serial, process='', contains='', min_level='info', max_lines=60):
    order = 'VDIWEF'
    floor = {'debug': 'D', 'info': 'I', 'notice': 'I', 'error': 'E', 'fault': 'F'}.get(min_level.lower(), 'I')
    out = adb(serial, 'logcat', '-d', '-v', 'brief', '-t', '800', timeout=40)
    rows = []
    for line in out.splitlines():
        m = re.match(r'^([VDIWEF])/(.+?)\(\s*(\d+)\):\s*(.*)$', line)
        if not m or order.index(m.group(1)) < order.index(floor): continue
        level, tag, pid, msg = m.groups()
        if process and process.casefold() not in tag.casefold(): continue
        if contains and contains.casefold() not in msg.casefold(): continue
        rows.append(dict(process=tag, pid=int(pid), level=level, message=msg[:300]))
    return dict(lines=rows[-max(1, min(int(max_lines), 300)):], note='Recent logcat lines (not a live stream).')


def app_performance(serial, top=10):
    out = adb(serial, 'shell', 'top', '-b', '-n', '1', '-m', top, timeout=30)
    return dict(raw=[l for l in out.splitlines() if l.strip()][:top + 6])
