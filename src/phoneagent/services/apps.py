"""Installed apps, launching apps, deep links."""
import re
from urllib.parse import urlsplit

from .. import policy
from .common import lockdown, run_devicectl

# Deep-link schemes mapped to the app that handles them. Unknown schemes need an explicit bundle_id.
SCHEMES = {
    'http': 'com.apple.mobilesafari', 'https': 'com.apple.mobilesafari',
    'whatsapp': 'net.whatsapp.WhatsApp', 'sms': 'com.apple.MobileSMS', 'tel': 'com.apple.mobilephone',
    'mailto': 'com.apple.mobilemail', 'maps': 'com.apple.Maps', 'facetime': 'com.apple.facetime',
    'fb-messenger': 'com.facebook.Messenger', 'tg': 'ph.telegra.Telegraph', 'spotify': 'com.spotify.client',
    'x-apple-reminderkit': 'com.apple.reminders', 'music': 'com.apple.Music',
}
BUNDLE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.\-_]{2,200}$')


async def list_apps(udid, kind='User'):
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    proxy = InstallationProxyService(await lockdown(udid))
    apps = await proxy.get_apps(application_type=kind)
    rows = [dict(bundle_id=b, name=a.get('CFBundleDisplayName') or a.get('CFBundleName'),
                 version=a.get('CFBundleShortVersionString'), type=a.get('ApplicationType'))
            for b, a in apps.items()]
    return sorted(rows, key=lambda r: (r['name'] or r['bundle_id']).casefold())


def launch(udid, bundle_id, url=None, restart=False):
    if not BUNDLE.match(bundle_id): raise ValueError('Invalid bundle id')
    allowed, why = policy.app_allowed(bundle_id)
    if not allowed: raise ValueError('%s %s' % (bundle_id, why))
    extra = []
    if restart: extra.append('--terminate-existing')
    if url: extra += ['--payload-url', url]
    run_devicectl(udid, 'process', 'launch', *extra, bundle_id, timeout=45)
    return 'Launched %s%s' % (bundle_id, ' with the URL' if url else '')


def open_url(udid, url, bundle_id=None):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if not scheme or len(url) > 2000: raise ValueError('Not a URL')
    if bundle_id is None:
        if scheme not in SCHEMES:
            raise ValueError('Unknown URL scheme %r: pass the bundle_id of the app that should open it. Known: %s'
                             % (scheme, ', '.join(sorted(SCHEMES))))
        bundle_id = SCHEMES[scheme]
    return launch(udid, bundle_id, url=url)


# Apps that come in more than one variant on the same phone: the human must choose which one to use.
VARIANTS = {
    'whatsapp': [dict(label='Personal', bundle_id='net.whatsapp.WhatsApp', scheme='whatsapp'),
                 dict(label='Business', bundle_id='net.whatsapp.WhatsAppSMB', scheme='whatsapp-smb')],
}


async def installed_variants(udid, family):
    installed = {a['bundle_id'] for a in await list_apps(udid, 'Any')}
    return [v for v in VARIANTS[family] if v['bundle_id'] in installed]
