"""Watch public homepages the operator types in. They do not have to be our sites.

Each pass downloads the homepage and records questionnaire mode, WhatsApp
numbers, and any other content change. A check of our own landing page is not
used; this reads whatever the domain serves to the public.
"""
import hashlib
import ipaddress
import logging
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx

from .redirects import PageParser, Scanner, _whatsapp_phone_at, read_direct_mode
from .sites import normalize_domain

LOGGER = logging.getLogger('ablab.detect')
MAX_DOMAINS = 20
MAX_HTML = 1_200_000
QUESTIONNAIRE_LABELS = {
    'offline': '网页打不开',
    '': '没有问卷开关',
    'ABC': '已跳过问卷',
    'CBA': '先出问卷',
    'ABCABC': '三连跳',
    'unrecognized': '有开关但认不出',
}
_FOLD_TOKEN = 'MODE'
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
_lock = threading.Lock()


class FetchError(ValueError):
    """A public page could not be read. The message is safe to show."""


def ensure_tables(registry):
    with registry.connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS detect_targets(
                domain TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS detect_snapshots(
                domain TEXT PRIMARY KEY, questionnaire TEXT NOT NULL, phones TEXT NOT NULL,
                digest TEXT NOT NULL, phones_known INTEGER NOT NULL, seen REAL NOT NULL,
                note TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS detect_events(
                id INTEGER PRIMARY KEY, domain TEXT NOT NULL, created REAL NOT NULL,
                kind TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL, detail TEXT NOT NULL);
        ''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(detect_snapshots)')}
        if columns and 'note' not in columns:
            db.execute("ALTER TABLE detect_snapshots ADD COLUMN note TEXT NOT NULL DEFAULT ''")


def parse_domain(value):
    if not isinstance(value, str):
        raise ValueError('请输入一个域名')
    text = value.strip()
    if '://' in text:
        parts = urlsplit(text)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError('请输入域名，不要带账号或奇怪的地址')
        text = parts.hostname
    return normalize_domain(text)


def _label(code):
    return QUESTIONNAIRE_LABELS.get(code, '没有问卷开关')


def _require_public_address(raw):
    text = raw.split('%', 1)[0]
    if text.startswith('::ffff:'):
        text = text.split('::ffff:', 1)[1]
    address = ipaddress.ip_address(text)
    if not address.is_global:
        raise FetchError('这个域名指向内网，没有打开')


def _require_public_host(host):
    host = host.strip('[]').lower()
    if not host or ':' in host:
        raise FetchError('这个地址不能打开')
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError('这个域名没有解析') from None
    seen = []
    for info in infos:
        raw = info[4][0]
        _require_public_address(raw)
        seen.append(raw)
    if not seen:
        raise FetchError('这个域名没有解析')


def _fetch_url(url):
    current = url
    for _hop in range(5):
        parts = urlsplit(current)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise FetchError('跳转到了不能打开的地址')
        if parts.port not in (None, 80, 443):
            raise FetchError('只打开网站的 80 或 443 端口')
        _require_public_host(parts.hostname)
        try:
            with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
                with client.stream('GET', current, headers={'User-Agent': _UA, 'Accept': 'text/html,application/xhtml+xml'}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('location') or ''
                        if not location:
                            raise FetchError('网页跳转没有目标')
                        nxt = urlsplit(location)
                        if not nxt.scheme:
                            current = str(response.url.join(location))
                        else:
                            current = location
                        continue
                    if response.status_code != 200:
                        raise FetchError(f'网页返回 {response.status_code}')
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_HTML:
                            raise FetchError('网页太大，没有继续读')
                        chunks.append(chunk)
                    body = b''.join(chunks)
        except FetchError:
            raise
        except httpx.HTTPError:
            raise FetchError('网页打不开') from None
        if body[:200].find(b'\x00') >= 0:
            raise FetchError('这不是网页')
        if body.startswith(b'\xef\xbb\xbf'):
            body = body[3:]
        return body.decode('utf-8', 'replace')
    raise FetchError('网页跳转次数太多')


def fetch_public_html(domain):
    """Download the public homepage. HTTPS first, then plain HTTP."""
    error = FetchError('网页打不开')
    for url in (f'https://{domain}/', f'http://{domain}/'):
        try:
            return _fetch_url(url)
        except FetchError as caught:
            error = caught
    raise error


def _fold_mode(text):
    from .redirects import _CONTACT_FALLBACK, _DIRECT_MODE

    def contact(match):
        start = match.start(4) - match.start()
        end = match.end(4) - match.start()
        return match.group(0)[:start] + _FOLD_TOKEN + match.group(0)[end:]

    def direct(match):
        return f"{match.group(1)}{match.group(2)}{_FOLD_TOKEN}{match.group(2)}"

    return _DIRECT_MODE.sub(direct, _CONTACT_FALLBACK.sub(contact, text))


def _fold_phones(text, phones):
    for phone in phones:
        text = re.sub(r'(?<!\d)' + re.escape(phone) + r'(?!\d)', 'PHONE', text)
    return text


def _questionnaire(text):
    try:
        return read_direct_mode(text) or ''
    except ValueError:
        return 'unrecognized'


def _phones(text):
    scanner = Scanner('index.html', text)
    try:
        PageParser(scanner).feed(text)
    except ValueError:
        return None
    found = set()
    for item in scanner.spans:
        url = item.get('url') or ''
        if item.get('kind') == 'whatsapp_number' and re.fullmatch(r'\d{8,15}', url):
            found.add(url)
        located = _whatsapp_phone_at(url)
        if located:
            found.add(located[2])
    return tuple(sorted(found))


def snapshot_html(text):
    phones = _phones(text)
    known = phones is not None
    used = phones or ()
    folded = _fold_phones(_fold_mode(text), used)
    code = _questionnaire(text)
    return {
        'questionnaire': code,
        'label': _label(code),
        'phones': ','.join(used) if known else '',
        'phones_known': known,
        'digest': hashlib.sha256(folded.encode()).hexdigest(),
        'note': '',
    }


def _show_phones(value):
    parts = [part for part in (value or '').split(',') if part]
    return '、'.join(parts) if parts else '无'


def _changes(previous, current):
    found = []
    if previous['questionnaire'] not in ('', 'offline') and previous['questionnaire'] != current['questionnaire']:
        before, after = _label(previous['questionnaire']), current['label']
        found.append(('questionnaire', before, after, f'{before} → {after}'))
    elif previous['questionnaire'] in ('', 'offline') and current['questionnaire'] not in ('', 'offline') and previous['questionnaire'] != current['questionnaire']:
        before, after = _label(previous['questionnaire']), current['label']
        found.append(('questionnaire', before, after, f'{before} → {after}'))
    if previous['phones_known'] and current['phones_known'] and previous['phones'] != current['phones'] and previous['questionnaire'] != 'offline':
        before, after = _show_phones(previous['phones']), _show_phones(current['phones'])
        found.append(('phone', before, after, f'{before} → {after}'))
    if previous['digest'] and current['digest'] and previous['digest'] != current['digest'] and previous['questionnaire'] != 'offline':
        found.append(('other', '上一版', '当前版', '首页除问卷和号码外有改动'))
    return found


def _baseline_detail(current):
    phones = _show_phones(current['phones']) if current['phones_known'] else '暂时读不出'
    return f"问卷：{current['label']}。号码：{phones}。"


def _offline(reason):
    return {
        'questionnaire': 'offline', 'label': '网页打不开', 'phones': '',
        'phones_known': True, 'digest': '', 'note': reason[:180],
    }


def _record(db, domain, now, kind, before, after, detail):
    db.execute(
        'INSERT INTO detect_events(domain, created, kind, before, after, detail) VALUES(?,?,?,?,?,?)',
        (domain, now, kind, before, after, detail[:400]),
    )


def _store_snapshot(db, domain, current, now):
    db.execute(
        '''INSERT INTO detect_snapshots(domain, questionnaire, phones, digest, phones_known, seen, note)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(domain) DO UPDATE SET
             questionnaire=excluded.questionnaire, phones=excluded.phones, digest=excluded.digest,
             phones_known=excluded.phones_known, seen=excluded.seen, note=excluded.note''',
        (domain, current['questionnaire'], current['phones'], current['digest'], int(current['phones_known']), now, current['note']),
    )


def check_detect(registry, fetch=None, only=None):
    """Compare each watched homepage with its last snapshot and append any changes."""
    ensure_tables(registry)
    fetch = fetch or fetch_public_html
    now = time.time()
    with _lock:
        with registry.connect() as db:
            targets = [row['domain'] for row in db.execute('SELECT domain FROM detect_targets ORDER BY created, domain')]
            if only:
                targets = [domain for domain in targets if domain == only]
        for domain in targets:
            try:
                current = snapshot_html(fetch(domain))
            except FetchError as error:
                current = _offline(str(error))
            except Exception as error:
                LOGGER.error('detect snapshot failed for %s (%s)', domain, type(error).__name__)
                current = _offline('网页打不开')
            with registry.connect() as db:
                row = db.execute(
                    'SELECT questionnaire, phones, digest, phones_known, note FROM detect_snapshots WHERE domain=?',
                    (domain,),
                ).fetchone()
                if row is None:
                    kind = 'fetch' if current['questionnaire'] == 'offline' else 'baseline'
                    detail = current['note'] if kind == 'fetch' else _baseline_detail(current)
                    _record(db, domain, now, kind, '', current['label'], detail)
                elif current['questionnaire'] == 'offline':
                    if row['questionnaire'] != 'offline':
                        _record(db, domain, now, 'fetch', _label(row['questionnaire']), '网页打不开', current['note'] or '网页打不开')
                    current = {
                        'questionnaire': 'offline', 'phones': row['phones'], 'digest': row['digest'],
                        'phones_known': bool(row['phones_known']), 'note': current['note'], 'label': '网页打不开',
                    }
                else:
                    previous = {
                        'questionnaire': row['questionnaire'], 'phones': row['phones'],
                        'digest': row['digest'], 'phones_known': bool(row['phones_known']),
                    }
                    for kind, before, after, detail in _changes(previous, current):
                        _record(db, domain, now, kind, before, after, detail)
                _store_snapshot(db, domain, current, now)
        with registry.connect() as db:
            db.execute('DELETE FROM detect_events WHERE id <= (SELECT MAX(id)-1000 FROM detect_events)')
    return read_detect(registry)


def add_target(registry, domain, fetch=None):
    ensure_tables(registry)
    with registry.connect() as db:
        count = db.execute('SELECT COUNT(*) FROM detect_targets').fetchone()[0]
        exists = db.execute('SELECT 1 FROM detect_targets WHERE domain=?', (domain,)).fetchone()
        if not exists and count >= MAX_DOMAINS:
            raise ValueError(f'最多同时监控 {MAX_DOMAINS} 个域名')
        db.execute('INSERT OR IGNORE INTO detect_targets(domain, created) VALUES(?,?)', (domain, time.time()))
    return check_detect(registry, fetch, only=domain)


def remove_target(registry, domain):
    ensure_tables(registry)
    domain = parse_domain(domain)
    with registry.connect() as db:
        db.execute('DELETE FROM detect_targets WHERE domain=?', (domain,))
        db.execute('DELETE FROM detect_snapshots WHERE domain=?', (domain,))
    return read_detect(registry)


def read_detect(registry):
    ensure_tables(registry)
    with registry.connect() as db:
        targets = []
        for row in db.execute('''
            SELECT t.domain, t.created, s.questionnaire, s.phones, s.seen, s.note
            FROM detect_targets t
            LEFT JOIN detect_snapshots s ON s.domain = t.domain
            ORDER BY t.created, t.domain
        '''):
            code = row['questionnaire'] if row['questionnaire'] is not None else ''
            targets.append({
                'domain': row['domain'],
                'created': row['created'],
                'questionnaire': code,
                'questionnaire_label': _label(code) if row['seen'] else '还没检查',
                'phones': _show_phones(row['phones'] or ''),
                'seen': row['seen'] or 0,
                'note': row['note'] or '',
            })
        events = [dict(item) for item in db.execute(
            'SELECT domain, created, kind, before, after, detail FROM detect_events ORDER BY id DESC LIMIT 100')]
    return {'targets': targets, 'events': events}


class DetectWatch:
    """Keep checking after the browser tab is closed."""

    def __init__(self, registry, interval=60, fetch=None):
        self.registry = registry
        self.interval = interval
        self.fetch = fetch
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='detect-mode', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                check_detect(self.registry, self.fetch)
            except Exception as error:
                LOGGER.error('detect watch pass failed (%s)', type(error).__name__)
            self._stop.wait(self.interval)
