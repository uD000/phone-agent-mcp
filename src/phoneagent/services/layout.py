"""Home Screen layout: read, summarize, sort (by name or icon color), write with backup, undo.

The layout is a list: [dock, page1, page2, ...]. An item is an app/web clip dict with displayIdentifier
and displayName, or a folder dict with displayName and iconLists (its pages). Every write is preceded by
a timestamped backup, and read back to verify.
"""
import colorsys
import math
import copy
import io
import json
import plistlib
import re
import time
import unicodedata
from datetime import datetime, timezone

from .common import HOME, lockdown, private_dir

BACKUPS = HOME / 'layouts'
COLOR_CACHE = HOME / 'cache' / 'icon-colors-v3.json'
PAGE_CAPACITY = 24      # 4 x 6 on current iPhones
FOLDER_CAPACITY = 9     # 3 x 3 per folder page


def is_folder(item): return isinstance(item, dict) and 'iconLists' in item
def is_icon(item): return isinstance(item, dict) and 'displayIdentifier' in item
def name_of(item): return str(item.get('displayName') or item.get('displayIdentifier') or '?')


def label(item):
    if is_folder(item):
        return dict(folder=name_of(item), items=[label(i) for page in item['iconLists'] for i in page])
    return name_of(item)


def summary(state):
    return dict(dock=[label(i) for i in state[0]], pages=[[label(i) for i in page] for page in state[1:]])


def top_items(state):
    return [i for page in state[1:] for i in page]


def all_icons(state):
    found = []
    for item in top_items(state):
        if is_folder(item): found += [i for page in item['iconLists'] for i in page if is_icon(i)]
        elif is_icon(item): found.append(item)
    return found


def check_sortable(state):
    odd = [str(i)[:60] for i in top_items(state) if not (is_icon(i) or is_folder(i))]
    if odd:
        raise ValueError('The layout has items I do not know how to place (widgets?): %s. Not sorting.' % odd[:3])


# ---- keys ------------------------------------------------------------------------------------
def name_key(item):
    text = unicodedata.normalize('NFKD', name_of(item)).encode('ascii', 'ignore').decode().casefold() or name_of(item)
    return [int(p) if p.isdigit() else p for p in re.split(r'(\d+)', text)]


def analyze(png):
    """Dominant hue and brightness of an icon image."""
    from PIL import Image
    with Image.open(io.BytesIO(png)) as image:
        image = image.convert('RGBA').resize((24, 24))
        pixels = list(image.getdata())
    bins, opaque, colored, value = [0.0] * 36, 0, 0, 0.0
    sx = sy = 0.0
    for r, g, b, a in pixels:
        if a < 200: continue
        opaque += 1
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        value += v
        if s > .25 and v > .25:
            colored += 1
            bins[int(h * 36) % 36] += s * v
            sx += s * v * math.cos(h * 2 * math.pi)
            sy += s * v * math.sin(h * 2 * math.pi)
    if not opaque: return dict(gray=True, value=0.0, hue=0.0, sat=0.0)
    mean_value = value / opaque
    if colored / opaque < .15 or sum(bins) == 0:
        return dict(gray=True, value=round(mean_value, 3), hue=0.0, sat=0.0)
    best = max(range(36), key=bins.__getitem__)
    # hue = dominant 30-degree neighbourhood, refined by a circular mean inside it (stable, continuous)
    near = [i % 36 for i in range(best - 1, best + 2)]
    nx = ny = 0.0
    for i in near:
        a = (i * 10 + 5) * math.pi / 180
        nx += bins[i] * math.cos(a); ny += bins[i] * math.sin(a)
    hue = (math.degrees(math.atan2(ny, nx)) + 360) % 360
    return dict(gray=False, value=round(mean_value, 3), hue=round(hue, 1), sat=round(bins[best] / max(colored, 1), 3))


def _info(png):
    info, bg = analyze(png), background(png)
    info.update(group=bg['group'], bg_hue=bg['hue'])
    return info


def background(png):
    """Hue/brightness of the icon's rim (what the eye sees as 'the icon color') -> colored, light or dark."""
    from PIL import Image
    with Image.open(io.BytesIO(png)) as image:
        image = image.convert('RGBA').resize((32, 32))
        px = image.load()
    rim = [px[x, y] for x in range(4, 28) for y in range(4, 28) if min(x, y, 31 - x, 31 - y) in (4, 5)]
    rim = [(r, g, b) for r, g, b, a in rim if a >= 200]
    if not rim: return dict(group='dark', hue=0.0)
    hsv = [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255) for r, g, b in rim]
    sx = sum(s * v * math.cos(h * 2 * math.pi) for h, s, v in hsv)
    sy = sum(s * v * math.sin(h * 2 * math.pi) for h, s, v in hsv)
    mean_s = sum(s for h, s, v in hsv) / len(hsv)
    mean_v = sum(v for h, s, v in hsv) / len(hsv)
    hue = (math.degrees(math.atan2(sy, sx)) + 360) % 360
    if mean_s > .35 and mean_v > .45: group = 'color'
    elif mean_v > .6: group = 'light'
    else: group = 'dark'
    return dict(group=group, hue=round(hue, 1))


def color_key(info):
    """Colored icons by background hue (rainbow), then light icons, then dark ones (each by logo hue)."""
    if info is None: return (3, 0, 0)
    group = info.get('group', 'dark')
    if group == 'color': return (0, (info['bg_hue'] + 20) % 360, 0)
    logo = 999 if info['gray'] else (info['hue'] + 20) % 360
    return (1 if group == 'light' else 2, logo, -info['value'])


def color_family(info):
    """Which page-group an icon belongs to when every color gets its own page."""
    if info is None: return 'other'
    group = info.get('group', 'dark')
    if group != 'color': return group
    r = (info['bg_hue'] + 20) % 360
    for limit, name in ((40, 'red'), (80, 'yellow'), (190, 'green'), (270, 'blue')):
        if r < limit: return name
    return 'purple'


def load_cache():
    try: return json.loads(COLOR_CACHE.read_text())
    except (OSError, ValueError): return {}


def save_cache(cache):
    private_dir(COLOR_CACHE.parent)
    COLOR_CACHE.write_text(json.dumps(cache))


async def icon_colors(sb, items):
    """Color info per displayIdentifier (cached on disk)."""
    cache, changed = load_cache(), False
    for item in items:
        ident = item.get('displayIdentifier')
        if not ident or ident in cache: continue
        try:
            png = await sb.get_icon_pngdata(ident)
            cache[ident] = _info(png) if png else None
        except Exception:
            cache[ident] = None
        changed = True
    if changed: save_cache(cache)
    return cache


def representative_color(item, cache):
    if is_folder(item):
        first = next((i for page in item['iconLists'] for i in page if is_icon(i)), None)
        return cache.get(first['displayIdentifier']) if first else None
    return cache.get(item.get('displayIdentifier'))


# ---- planning --------------------------------------------------------------------------------
def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def plan_sort(state, mode, cache=None, folders='first', inside_folders=False):
    """Return the new layout (dock untouched, folders optionally sorted inside)."""
    check_sortable(state)
    if mode not in ('name', 'color'): raise ValueError('mode must be name or color')
    if folders not in ('first', 'last', 'mixed'): raise ValueError('folders must be first, last or mixed')
    new = copy.deepcopy(state)
    items = top_items(new)

    def key(item):
        return name_key(item) if mode == 'name' else (color_key(representative_color(item, cache or {})), name_key(item))

    if inside_folders:
        for item in items:
            if is_folder(item):
                inner = sorted((i for page in item['iconLists'] for i in page), key=key)
                item['iconLists'] = chunk(inner, FOLDER_CAPACITY) or [[]]
    folder_items = sorted((i for i in items if is_folder(i)), key=name_key)
    icon_items = sorted((i for i in items if not is_folder(i)), key=key)
    if folders == 'mixed':
        ordered = sorted(items, key=key)
    elif folders == 'last':
        ordered = icon_items + folder_items
    else:
        ordered = folder_items + icon_items
    capacity = max(PAGE_CAPACITY, *(len(p) for p in state[1:])) if len(state) > 1 else PAGE_CAPACITY
    if mode == 'color' and folders != 'mixed':
        pages, groups = [], {}
        for item in icon_items:
            groups.setdefault(color_family(representative_color(item, cache or {})), []).append(item)
        for family in ('red', 'yellow', 'green', 'blue', 'purple', 'light', 'dark', 'other'):
            pages += chunk(groups.get(family, []), capacity)
        pages = chunk(folder_items, capacity) + pages if folders == 'first' else pages + chunk(folder_items, capacity)
        return [new[0]] + pages
    return [new[0]] + chunk(ordered, capacity)


def moved_count(old, new):
    where = lambda state: {name_of(i) + '|' + str(i.get('displayIdentifier')): (p, n)
                           for p, page in enumerate(state[1:]) for n, i in enumerate(page)}
    a, b = where(old), where(new)
    return sum(1 for k in a if a[k] != b.get(k))


# ---- device I/O ------------------------------------------------------------------------------
async def service(udid):
    from pymobiledevice3.services.springboard import SpringBoardServicesService
    return SpringBoardServicesService(await lockdown(udid))


async def get_layout(udid):
    sb = await service(udid)
    return await sb.get_icon_state()


def save_backup(state, tag):
    private_dir(BACKUPS)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')[:-3]
    path = BACKUPS / f'{stamp}-{tag}.plist'
    with open(path, 'wb') as f: plistlib.dump(state, f)
    path.chmod(0o600)
    return path


def list_backups():
    return sorted(p.name for p in BACKUPS.glob('*.plist')) if BACKUPS.exists() else []


async def write_layout(udid, new_state, tag):
    """Back up the current layout, write the new one, read it back. Returns a result dict."""
    sb = await service(udid)
    current = await sb.get_icon_state()
    backup = save_backup(current, tag)
    started = time.monotonic()
    await sb.set_icon_state(new_state)
    after = await sb.get_icon_state()
    verified = after == new_state or summary(after) == summary(new_state)
    return dict(applied=True, verified=verified, backup=backup.name, seconds=round(time.monotonic() - started, 2))


async def sort_layout(udid, mode, apply, folders='first', inside_folders=False):
    sb = await service(udid)
    state = await sb.get_icon_state()
    cache = await icon_colors(sb, all_icons(state)) if mode == 'color' else None
    new = plan_sort(state, mode, cache, folders, inside_folders)
    result = dict(mode=mode, moved=moved_count(state, new), pages_before=len(state) - 1, pages_after=len(new) - 1,
                  preview=summary(new)['pages'], applied=False)
    if moved_count(state, new) == 0 and new == state:
        result['note'] = 'Already in this order'
        return result
    if apply:
        result.update(await write_layout(udid, new, 'before-sort-' + mode))
        result.pop('preview')
    return result


async def undo(udid, backup=None):
    names = list_backups()
    if not names: raise ValueError('No layout backups yet')
    name = backup or names[-1]
    if name not in names: raise ValueError('Unknown backup. Available: ' + ', '.join(names[-10:]))
    with open(BACKUPS / name, 'rb') as f: state = plistlib.load(f)
    result = await write_layout(udid, state, 'before-undo')
    result['restored'] = name
    return result
