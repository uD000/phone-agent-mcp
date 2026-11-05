"""Eyes on the phone: network traffic per app (pcapd, USB only), system logs, performance, battery.

Traffic shows who talks to whom (apps, domains, bytes, timing). HTTPS contents are encrypted and are not visible.
Results can reveal what the human does on the phone, so callers keep them out of the audit log.
"""
import asyncio
import ipaddress
import struct
import time
from collections import defaultdict

from .common import lockdown

MAX_SECONDS = 120


# ---- packet parsing --------------------------------------------------------------------------------
def _is_private(address):
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_private or ip.is_link_local or ip.is_loopback
    except ValueError:
        return False


def _dns_name(data, offset, depth=0):
    """Read a DNS name (with compression pointers). Returns (name, next_offset)."""
    labels, jumped, end = [], False, offset
    while depth < 20 and offset < len(data):
        n = data[offset]
        if n == 0:
            offset += 1
            break
        if n & 0xC0 == 0xC0:
            if offset + 1 >= len(data): break
            pointer = ((n & 0x3F) << 8) | data[offset + 1]
            if not jumped: end = offset + 2
            jumped, offset, depth = True, pointer, depth + 1
            continue
        labels.append(data[offset + 1:offset + 1 + n].decode('ascii', 'ignore'))
        offset += 1 + n
    if not jumped: end = offset
    return '.'.join(labels), end


def parse_dns(payload):
    """(query names, {ip: name} from answers) of a DNS message; empty if it does not parse."""
    names, answers = [], {}
    try:
        if len(payload) < 12: return names, answers
        qd, an = struct.unpack('!HH', payload[4:8])
        offset = 12
        for _ in range(min(qd, 4)):
            name, offset = _dns_name(payload, offset)
            names.append(name)
            offset += 4
        for _ in range(min(an, 20)):
            name, offset = _dns_name(payload, offset)
            rtype, _cls, _ttl, rdlen = struct.unpack('!HHIH', payload[offset:offset + 10])
            offset += 10
            rdata = payload[offset:offset + rdlen]
            offset += rdlen
            if rtype == 1 and rdlen == 4: answers[str(ipaddress.IPv4Address(rdata))] = name
            elif rtype == 28 and rdlen == 16: answers[str(ipaddress.IPv6Address(rdata))] = name
    except (struct.error, ValueError, IndexError):
        pass
    return names, answers


def parse_sni(payload):
    """Server name from a TLS ClientHello, or None."""
    try:
        if len(payload) < 45 or payload[0] != 0x16 or payload[5] != 0x01: return None
        pos = 9 + 2 + 32                       # record hdr(5) + handshake hdr(4) + version(2) + random(32)
        pos += 1 + payload[pos]                # session id
        pos += 2 + struct.unpack('!H', payload[pos:pos + 2])[0]   # cipher suites
        pos += 1 + payload[pos]                # compression
        end = pos + 2 + struct.unpack('!H', payload[pos:pos + 2])[0]
        pos += 2
        while pos + 4 <= min(end, len(payload)):
            ext, size = struct.unpack('!HH', payload[pos:pos + 4])
            pos += 4
            if ext == 0 and size >= 5:
                name_len = struct.unpack('!H', payload[pos + 3:pos + 5])[0]
                return payload[pos + 5:pos + 5 + name_len].decode('ascii', 'ignore') or None
            pos += size
    except (struct.error, IndexError):
        pass
    return None


def parse_frame(frame):
    """Ethernet frame -> dict(proto, src, dst, sport, dport, size, payload) or None."""
    if len(frame) < 34: return None
    ethertype = struct.unpack('!H', frame[12:14])[0]
    ip = frame[14:]
    try:
        if ethertype == 0x0800 and len(ip) >= 20:
            ihl = (ip[0] & 0x0F) * 4
            proto, total = ip[9], struct.unpack('!H', ip[2:4])[0]
            src, dst = str(ipaddress.IPv4Address(ip[12:16])), str(ipaddress.IPv4Address(ip[16:20]))
            l4, size = ip[ihl:total or len(ip)], max(total, 0)
        elif ethertype == 0x86DD and len(ip) >= 40:
            proto, plen = ip[6], struct.unpack('!H', ip[4:6])[0]
            src, dst = str(ipaddress.IPv6Address(ip[8:24])), str(ipaddress.IPv6Address(ip[24:40]))
            l4, size = ip[40:40 + plen], plen + 40
        else:
            return None
    except (ValueError, struct.error):
        return None
    sport = dport = 0
    payload = b''
    if proto == 6 and len(l4) >= 20:
        sport, dport = struct.unpack('!HH', l4[:4])
        payload = l4[((l4[12] >> 4) * 4):]
    elif proto == 17 and len(l4) >= 8:
        sport, dport = struct.unpack('!HH', l4[:4])
        payload = l4[8:]
    return dict(proto={6: 'tcp', 17: 'udp', 1: 'icmp', 58: 'icmp6'}.get(proto, str(proto)), src=src, dst=dst,
                sport=sport, dport=dport, size=size, payload=payload)


class TrafficSummary:
    def __init__(self):
        self.procs = defaultdict(lambda: dict(packets=0, out=0, inn=0, flows=defaultdict(lambda: dict(out=0, inn=0, names=set()))))
        self.dns = {}
        self.device_ips = set()
        self.total = 0

    def add(self, comm, io, frame):
        info = parse_frame(frame)
        if not info: return
        self.total += 1
        src, dst = info['src'], info['dst']
        names = ()
        if info['proto'] == 'udp' and 53 in (info['sport'], info['dport']):
            queries, answers = parse_dns(info['payload'])
            self.dns.update(answers)
            names = queries
        sni = parse_sni(info['payload']) if info['proto'] == 'tcp' and info['payload'][:1] == b'\x16' else None
        # direction: the device is the private side talking to a public one; fall back to the capture flag
        if src in self.device_ips or (_is_private(src) and not _is_private(dst)):
            outbound = True
            self.device_ips.add(src)
        elif dst in self.device_ips or (_is_private(dst) and not _is_private(src)):
            outbound = False
            self.device_ips.add(dst)
        else:
            outbound = io == 1
        remote, rport = (dst, info['dport']) if outbound else (src, info['sport'])
        proc = self.procs[comm or '?']
        proc['packets'] += 1
        flow = proc['flows'][(remote, rport, info['proto'])]
        if outbound: proc['out'] += info['size']; flow['out'] += info['size']
        else: proc['inn'] += info['size']; flow['inn'] += info['size']
        if sni: flow['names'].add(sni)
        for name in names: flow['names'].add(name)

    def report(self, top_procs=8, top_hosts=5):
        rows = []
        for comm, proc in sorted(self.procs.items(), key=lambda kv: -(kv[1]['out'] + kv[1]['inn']))[:top_procs]:
            hosts = []
            for (remote, rport, proto), flow in sorted(proc['flows'].items(), key=lambda kv: -(kv[1]['out'] + kv[1]['inn']))[:top_hosts]:
                name = sorted(flow['names'])[0] if flow['names'] else self.dns.get(remote)
                hosts.append(dict(host=name or remote, ip=remote, port=rport, proto=proto, bytes_out=flow['out'], bytes_in=flow['inn']))
            rows.append(dict(process=comm, packets=proc['packets'], bytes_out=proc['out'], bytes_in=proc['inn'], top_hosts=hosts))
        return dict(packets=self.total, processes=rows,
                    note='Metadata only: HTTPS contents are encrypted. Names come from TLS SNI and DNS.')


async def capture_traffic(udid, seconds, process=None, rsd=None):
    """Capture for `seconds` and summarize per process. Pass the session's RSD tunnel (USB or Wi-Fi, iOS 17+) as rsd;
    without it the older lockdown service is used, which only works over USB."""
    from pymobiledevice3.services.pcapd import PcapdService
    seconds = max(1.0, min(float(seconds), MAX_SECONDS))
    try:
        service = PcapdService(rsd if rsd is not None else await lockdown(udid))
        stream = service.watch(process=process or None)
    except Exception as error:
        raise OSError('Traffic capture could not start (%s). Without a live connection it needs USB.' % (str(error) or type(error).__name__)) from error
    summary, deadline = TrafficSummary(), time.monotonic() + seconds
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0: break
            try:
                packet = await asyncio.wait_for(stream.__anext__(), timeout=left)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            summary.add(packet.comm, packet.io, packet.data)
    except Exception as error:
        if summary.total == 0:
            raise OSError('Traffic capture failed: %s' % (str(error) or type(error).__name__)) from error
    finally:
        try: await stream.aclose()
        except Exception: pass
    report = summary.report()
    await _reverse_dns([h for r in report['processes'] for h in r['top_hosts'] if h['host'] == h['ip']])
    report['note'] += ' Hostnames that are not TLS/DNS names come from reverse DNS (best effort).'
    return dict(report, seconds=seconds)


# ---- logs ------------------------------------------------------------------------------------------
LEVELS = {'debug': 0, 'info': 1, 'notice': 2, 'error': 3, 'fault': 4}


def _rank(level):
    name = getattr(level, 'name', str(level)).lower()
    return {'debug': 0, 'info': 1, 'notice': 2, 'user_action': 2, 'error': 3, 'fault': 4}.get(name, 1)


async def tail_logs(udid, seconds, process='', contains='', min_level='info', max_lines=80, rsd=None):
    from pymobiledevice3.services.os_trace import OsTraceService
    seconds, max_lines = max(1.0, min(float(seconds), 60)), max(1, min(int(max_lines), 300))
    floor = LEVELS.get(min_level.lower(), 1)
    process, contains = process.casefold(), contains.casefold()
    service = OsTraceService(rsd if rsd is not None else await lockdown(udid))
    stream = service.syslog()
    rows, deadline, seen = [], time.monotonic() + seconds, 0
    try:
        while len(rows) < max_lines:
            left = deadline - time.monotonic()
            if left <= 0: break
            try:
                entry = await asyncio.wait_for(stream.__anext__(), timeout=left)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            seen += 1
            who = (entry.filename or entry.image_name or '').rsplit('/', 1)[-1]
            if process and process not in who.casefold(): continue
            if _rank(entry.level) < floor: continue
            if contains and contains not in entry.message.casefold(): continue
            rows.append(dict(time=entry.timestamp.strftime('%H:%M:%S'), process=who, pid=entry.pid,
                             level=getattr(entry.level, 'name', str(entry.level)).lower(), message=entry.message[:300]))
    finally:
        try: await stream.aclose()
        except Exception: pass
    return dict(lines=rows, scanned=seen, seconds=seconds)


# ---- device, battery, performance -------------------------------------------------------------------
async def device_info(udid):
    ld = await lockdown(udid)
    get = lambda key, domain=None: asyncio.ensure_future(ld.get_value(domain=domain, key=key))
    values = {}
    for label, key, domain in [('name', 'DeviceName', None), ('model', 'ProductType', None), ('ios', 'ProductVersion', None),
                               ('storage_total_bytes', 'TotalDiskCapacity', 'com.apple.disk_usage'),
                               ('storage_free_bytes', 'AmountDataAvailable', 'com.apple.disk_usage'),
                               ('battery_percent', 'BatteryCurrentCapacity', 'com.apple.mobile.battery'),
                               ('charging', 'BatteryIsCharging', 'com.apple.mobile.battery')]:
        try: values[label] = await ld.get_value(domain=domain, key=key)
        except Exception: values[label] = None
    return values


async def battery(udid):
    from pymobiledevice3.services.diagnostics import DiagnosticsService
    info = await DiagnosticsService(await lockdown(udid)).get_battery() or {}
    keep = ('CurrentCapacity', 'MaxCapacity', 'DesignCapacity', 'CycleCount', 'Temperature', 'IsCharging', 'ExternalConnected',
            'AppleRawCurrentCapacity', 'AppleRawMaxCapacity', 'NominalChargeCapacity', 'Voltage', 'InstantAmperage')
    out = {k: info[k] for k in keep if k in info}
    if 'Temperature' in out: out['temperature_c'] = round(out['Temperature'] / 100, 1)
    return out


async def app_performance(rsd, seconds=3, top=10):
    """Top processes by CPU and memory from Instruments sysmontap, over the session's tunnel."""
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
    seconds = max(1.0, min(float(seconds), 20))
    samples = []
    async with DvtProvider(rsd) as dvt:
        tap = await Sysmontap.create(dvt, interval=1000)
        async with tap:
            deadline = time.monotonic() + seconds + 1.5
            stream = tap.iter_processes()
            while time.monotonic() < deadline and len(samples) < max(2, int(seconds)):
                try:
                    samples.append(await asyncio.wait_for(stream.__anext__(), timeout=max(0.5, deadline - time.monotonic())))
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
    # the first sample has no CPU baseline; average the rest
    use = samples[1:] or samples
    agg = defaultdict(lambda: dict(cpu=0.0, mem=0, n=0))
    for sample in use:
        for p in sample:
            name = p.get('name') or str(p.get('pid'))
            a = agg[name]
            a['cpu'] += float(p.get('cpuUsage') or 0)
            a['mem'] = max(a['mem'], int(p.get('physFootprint') or 0))
            a['n'] += 1
    rows = [dict(process=n, cpu_percent=round(a['cpu'] / max(a['n'], 1), 1), memory_mb=round(a['mem'] / 1048576, 1)) for n, a in agg.items()]
    return dict(samples=len(use), by_cpu=sorted(rows, key=lambda r: -r['cpu_percent'])[:top],
                by_memory=sorted(rows, key=lambda r: -r['memory_mb'])[:top])


# ---- connections per app (works over Wi-Fi too): Instruments network monitor ----------------------------------
async def connections_summary(rsd, seconds=8, top_procs=8, top_hosts=5):
    """Which app opened which connections and how many bytes moved, without mirroring packets. Hosts are IPs
    (best-effort reverse DNS added); there are no TLS names here, unlike the USB packet capture."""
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
    from pymobiledevice3.services.dvt.instruments.network_monitor import (
        ConnectionDetectionEvent, ConnectionUpdateEvent, NetworkMonitor)
    seconds = max(2.0, min(float(seconds), MAX_SECONDS))
    conns, stats = {}, {}
    async with DvtProvider(rsd) as dvt:
        async with DeviceInfo(dvt) as info:
            names = {p.get('pid'): (p.get('name') or p.get('realAppName')) for p in await info.proclist()}
        async with NetworkMonitor(dvt) as monitor:
            stream = monitor.__aiter__()
            deadline = time.monotonic() + seconds
            while True:
                left = deadline - time.monotonic()
                if left <= 0: break
                try:
                    event = await asyncio.wait_for(stream.__anext__(), timeout=left)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
                if isinstance(event, ConnectionDetectionEvent):
                    remote = event.remote_address
                    conns[event.serial_number] = dict(pid=event.pid, ip=str(remote.address) if remote.address else '?', port=remote.port)
                elif isinstance(event, ConnectionUpdateEvent):
                    stats[event.connection_serial] = (event.tx_bytes, event.rx_bytes)
        async with DeviceInfo(dvt) as info:
            for p in await info.proclist(): names.setdefault(p.get('pid'), p.get('name') or p.get('realAppName'))
    procs = defaultdict(lambda: dict(out=0, inn=0, flows={}))
    for serial, c in conns.items():
        tx, rx = stats.get(serial, (0, 0))
        if c['ip'] in ('?', '0.0.0.0', '::'): continue
        proc = procs[names.get(c['pid']) or 'pid %s' % c['pid']]
        proc['out'] += tx; proc['inn'] += rx
        key = (c['ip'], c['port'])
        flow = proc['flows'].setdefault(key, dict(out=0, inn=0, n=0))
        flow['out'] += tx; flow['inn'] += rx; flow['n'] += 1
    rows = []
    for name, proc in sorted(procs.items(), key=lambda kv: -(kv[1]['out'] + kv[1]['inn']))[:top_procs]:
        hosts = [dict(host=ip, ip=ip, port=port, connections=f['n'], bytes_out=f['out'], bytes_in=f['inn'])
                 for (ip, port), f in sorted(proc['flows'].items(), key=lambda kv: -(kv[1]['out'] + kv[1]['inn']))[:top_hosts]]
        rows.append(dict(process=name, bytes_out=proc['out'], bytes_in=proc['inn'], top_hosts=hosts))
    await _reverse_dns([h for r in rows for h in r['top_hosts']])
    return dict(connections=len(conns), processes=rows, seconds=seconds,
                note='Connections and bytes per app (no packet capture, so no TLS names). Hostnames are best-effort reverse DNS. '
                     'Only connections opened during the window are listed with their traffic.')


async def _reverse_dns(hosts, limit=15):
    import socket
    def lookup(ip):
        try: return socket.gethostbyaddr(ip)[0]
        except (OSError, ValueError): return None
    unique = list(dict.fromkeys(h['ip'] for h in hosts if _public(h['ip'])))[:limit]
    try:
        results = await asyncio.wait_for(asyncio.gather(*(asyncio.to_thread(lookup, ip) for ip in unique)), timeout=4)
    except asyncio.TimeoutError:
        return
    names = dict(zip(unique, results))
    for h in hosts:
        if names.get(h['ip']): h['host'] = names[h['ip']]


def _public(ip):
    try: return not _is_private(ip)
    except ValueError: return False


async def app_network_usage(rsd, seconds=8, top=10):
    """Bytes sent and received per app/process during the window (Instruments NetworkStatistics, works over Wi-Fi)."""
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
    from pymobiledevice3.services.dvt.instruments.network_statistics import NetworkStatistics
    seconds = max(2.0, min(float(seconds), MAX_SECONDS))
    async with DvtProvider(rsd) as dvt:
        async with DeviceInfo(dvt) as info:
            procs = await info.proclist()
        names = {p.get('pid'): (p.get('name') or p.get('realAppName')) for p in procs}
        apps_only = {p['pid'] for p in procs if p.get('isApplication')}
        pids = sorted((p['pid'] for p in procs if p.get('pid', 0) > 0), key=lambda x: x not in apps_only)[:400]
        first, last = {}, {}
        async with NetworkStatistics(dvt, pids, 1000) as stats:
            stream = stats.__aiter__()
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    sample = await asyncio.wait_for(stream.__anext__(), timeout=max(.5, deadline - time.monotonic()))
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
                for pid, values in sample.items():
                    first.setdefault(pid, values)
                    last[pid] = values
    rows = []
    for pid, end in last.items():
        start = first.get(pid, {})
        tx = int(end.get('net.tx.bytes', 0) or 0) - int(start.get('net.tx.bytes', 0) or 0)
        rx = int(end.get('net.rx.bytes', 0) or 0) - int(start.get('net.rx.bytes', 0) or 0)
        if tx or rx: rows.append(dict(process=names.get(pid) or 'pid %s' % pid, is_app=pid in apps_only, bytes_out=max(tx, 0), bytes_in=max(rx, 0)))
    return dict(apps=sorted(rows, key=lambda r: -(r['bytes_out'] + r['bytes_in']))[:top], seconds=seconds)


async def network_overview(rsd, seconds=8):
    """Per-app byte counts and per-host totals together. Over Wi-Fi iOS does not say which app owns a connection, so
    the two lists are separate; with a USB cable the packet capture attributes hosts to apps."""
    apps, hosts = await asyncio.gather(app_network_usage(rsd, seconds), connections_summary(rsd, seconds))
    hosts_flat = {}
    for p in hosts['processes']:
        for h in p['top_hosts']:
            row = hosts_flat.setdefault(h['ip'], dict(host=h['host'], ip=h['ip'], port=h['port'], bytes_out=0, bytes_in=0))
            row['bytes_out'] += h['bytes_out']; row['bytes_in'] += h['bytes_in']
    top_hosts = sorted(hosts_flat.values(), key=lambda r: -(r['bytes_out'] + r['bytes_in']))[:12]
    return dict(seconds=apps['seconds'], apps=apps['apps'], hosts=top_hosts, connections_seen=hosts['connections'],
                note='Apps and hosts are separate lists over Wi-Fi: iOS does not report which app owns a connection there. '
                     'Plug in a USB cable for per-app hosts and TLS names. HTTPS contents are never visible.')
