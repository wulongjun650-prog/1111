import hashlib
import hmac
import ipaddress
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import shutil
import sqlite3
import time

from .models import Config
from .archives import edit_content


class Conflict(ValueError):
    pass


def choose_split_member(members, mode):
    if mode == 'weighted':
        total = sum(item['weight'] for item in members)
        draw = secrets.randbelow(total)
        covered = 0
        for item in members:
            covered += item['weight']
            if draw < covered:
                return item
    return secrets.choice(members)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.pages = self.directory / 'pages'
        self.pages.mkdir(exist_ok=True)
        self.path = self.directory / 'app.db'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, revision INTEGER NOT NULL, secret TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY, slot TEXT NOT NULL, name TEXT NOT NULL, created REAL NOT NULL, files INTEGER NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS slots(slot TEXT PRIMARY KEY, version_id TEXT REFERENCES versions(id));
                CREATE TABLE IF NOT EXISTS counters(ip_key TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS links(id INTEGER PRIMARY KEY, slot TEXT NOT NULL, url TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0, UNIQUE(slot,url));
                CREATE TABLE IF NOT EXISTS rotation(slot TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS redirect_presets(id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, note TEXT NOT NULL, created REAL NOT NULL, check_result TEXT);
                CREATE TABLE IF NOT EXISTS whatsapp_numbers(id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL UNIQUE, note TEXT NOT NULL, created REAL NOT NULL, trust_result TEXT);
                CREATE TABLE IF NOT EXISTS redirect_active(id INTEGER PRIMARY KEY CHECK(id=1), url TEXT NOT NULL, preset_id INTEGER REFERENCES redirect_presets(id) ON DELETE SET NULL, version_id TEXT NOT NULL REFERENCES versions(id), updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS redirect_split(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, mode TEXT NOT NULL, members TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS redirect_split_assign(visitor_hash TEXT PRIMARY KEY, preset_id INTEGER, url TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS whatsapp_split(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, mode TEXT NOT NULL, members TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS whatsapp_split_assign(visitor_hash TEXT PRIMARY KEY, number_id INTEGER, phone TEXT NOT NULL, display_name TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS whatsapp_trust_state(id INTEGER PRIMARY KEY CHECK(id=1), seen_trust INTEGER NOT NULL DEFAULT 0, seen_clear INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, created REAL NOT NULL, ip TEXT NOT NULL, country TEXT, device TEXT NOT NULL, slot TEXT NOT NULL, reason TEXT NOT NULL, path TEXT NOT NULL, mode TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_created ON events(created);
                CREATE TABLE IF NOT EXISTS access_log(id INTEGER PRIMARY KEY, created REAL NOT NULL, ua TEXT NOT NULL, ch_ua TEXT NOT NULL, ch_platform TEXT NOT NULL, ch_platform_version TEXT NOT NULL, ch_mobile TEXT NOT NULL, referer TEXT NOT NULL, query TEXT NOT NULL, wa_number TEXT NOT NULL, cf_cache_status TEXT NOT NULL, country TEXT, city TEXT NOT NULL, status INTEGER NOT NULL, duration_ms INTEGER NOT NULL, path TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS access_log_created ON access_log(created);
                CREATE TABLE IF NOT EXISTS client_events(id INTEGER PRIMARY KEY, created REAL NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS client_events_created ON client_events(created);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, created REAL NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(events)')}
            if 'device_details' not in columns:
                db.execute('ALTER TABLE events ADD COLUMN device_details TEXT')
            number_columns = {row['name'] for row in db.execute('PRAGMA table_info(whatsapp_numbers)')}
            if 'trust_result' not in number_columns:
                db.execute('ALTER TABLE whatsapp_numbers ADD COLUMN trust_result TEXT')
            if 'display_name' not in number_columns:
                db.execute("ALTER TABLE whatsapp_numbers ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
            db.execute('INSERT OR IGNORE INTO settings VALUES(1,?,0,?)', (Config().model_dump_json(), secrets.token_hex(32)))
            db.executemany('INSERT OR IGNORE INTO slots(slot) VALUES(?)', [('A',), ('B',)])
            self.secret = db.execute('SELECT secret FROM settings WHERE id=1').fetchone()[0].encode()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def config(self):
        with self.connect() as db:
            row = db.execute('SELECT config,revision FROM settings WHERE id=1').fetchone()
        return Config.model_validate_json(row['config']), row['revision']

    def _audit(self, db, action, detail):
        db.execute('INSERT INTO audit(created,action,detail) VALUES(?,?,?)', (time.time(), action, json.dumps(detail, ensure_ascii=False)))
        db.execute('DELETE FROM audit WHERE id <= (SELECT MAX(id)-2000 FROM audit)')

    def save_config(self, config, revision):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT config,revision FROM settings WHERE id=1').fetchone()
            if old['revision'] != revision:
                raise Conflict('配置已被其他窗口修改，请刷新后重试')
            db.execute('UPDATE settings SET config=?,revision=revision+1 WHERE id=1', (config.model_dump_json(),))
            self._audit(db, 'config_updated', {'before': json.loads(old['config']), 'after': config.model_dump()})

    def add_version(self, slot, version):
        with self.connect() as db:
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)', (version['id'], slot, version['name'], version['created'], version['files'], version['bytes'], version['sha256']))
            self._audit(db, 'content_imported', {'slot': slot, 'version': version['id'], 'files': version['files']})

    def versions(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM versions ORDER BY created DESC')]

    def version(self, version_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone()
            return dict(row) if row else None

    def slots(self):
        with self.connect() as db:
            return {row['slot']: row['version_id'] for row in db.execute('SELECT * FROM slots')}

    def publish(self, slot, version_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('SELECT id FROM versions WHERE id=? AND slot=?', (version_id, slot)).fetchone()
            if not version or not (self.pages / version_id / 'index.html').is_file():
                raise ValueError('版本不存在、槽位不符或入口文件缺失')
            old = db.execute('SELECT version_id FROM slots WHERE slot=?', (slot,)).fetchone()[0]
            db.execute('UPDATE slots SET version_id=? WHERE slot=?', (version_id, slot))
            self._audit(db, 'version_published', {'slot': slot, 'before': old, 'after': version_id})

    def _version_directory(self, version_id):
        if not re.fullmatch(r'[a-f0-9]{32}', version_id or ''):
            raise ValueError('版本不存在或槽位不符')
        root = self.pages.resolve()
        target = (root / version_id).resolve()
        if target.parent != root:
            raise ValueError('版本不存在或槽位不符')
        return target

    def delete_version(self, version_id, slot='B'):
        """Hard-delete one unpublished A or B version and its files."""
        if slot not in ('A', 'B'):
            raise ValueError('只能删除未发布的 A 或 B 版本')
        target = self._version_directory(version_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('SELECT id, slot, name FROM versions WHERE id=?', (version_id,)).fetchone()
            if not version or version['slot'] != slot:
                raise ValueError(f'只能删除未发布的 {slot} 版本')
            published = db.execute('SELECT version_id FROM slots WHERE slot=?', (slot,)).fetchone()[0]
            if published == version_id:
                raise ValueError(f'不能删除当前发布的 {slot} 版本')
            active = db.execute('SELECT version_id FROM redirect_active WHERE id=1').fetchone()
            cleared_active = bool(active and active['version_id'] == version_id)
            if cleared_active:
                db.execute('DELETE FROM redirect_active WHERE id=1')
            if not db.execute('DELETE FROM versions WHERE id=? AND slot=?', (version_id, slot)).rowcount:
                raise ValueError(f'只能删除未发布的 {slot} 版本')
            self._audit(db, f'{slot.lower()}_version_deleted', {'version': version_id, 'slot': slot, 'name': version['name'], 'cleared_active': cleared_active})
        if target.exists():
            shutil.rmtree(target)
        return version_id

    def delete_unpublished_versions(self, slot):
        if slot not in ('A', 'B'):
            raise ValueError('只能删除未发布的 A 或 B 版本')
        with self.connect() as db:
            published = db.execute('SELECT version_id FROM slots WHERE slot=?', (slot,)).fetchone()[0]
            ids = [row['id'] for row in db.execute('SELECT id FROM versions WHERE slot=? AND (? IS NULL OR id != ?) ORDER BY created', (slot, published, published))]
        deleted = []
        for version_id in ids:
            try:
                self.delete_version(version_id, slot)
            except ValueError:
                continue
            deleted.append(version_id)
        return deleted

    def delete_unpublished_b_versions(self):
        return self.delete_unpublished_versions('B')

    def save_source(self, slot, base, path, content, expected_published):
        version = edit_content(self.pages / base['id'], path, content, self.pages, base['name'])
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT version_id FROM slots WHERE slot=?', (slot,)).fetchone()[0]
                if current != expected_published:
                    raise Conflict('当前发布版本已改变，请重新加载后再保存')
                db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)', (version['id'], slot, version['name'], version['created'], version['files'], version['bytes'], version['sha256']))
                db.execute('UPDATE slots SET version_id=? WHERE slot=?', (version['id'], slot))
                self._audit(db, 'content_imported', {'slot': slot, 'version': version['id'], 'files': version['files'], 'source_version': base['id'], 'path': path})
                self._audit(db, 'version_published', {'slot': slot, 'before': current, 'after': version['id']})
        except Exception:
            shutil.rmtree(self.pages / version['id'])
            raise
        return version | {'slot': slot}

    def count(self, ip, hours, increment, now=None):
        now = time.time() if now is None else now
        normalized = str(ipaddress.ip_address(ip))
        key = hmac.new(self.secret, normalized.encode(), hashlib.sha256).hexdigest()
        with self.connect() as db:
            if increment:
                db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT started,count FROM counters WHERE ip_key=?', (key,)).fetchone()
            active = row and now < row['started'] + hours * 3600
            count = row['count'] if active else 0
            if increment:
                count += 1
                started = row['started'] if active else now
                db.execute('INSERT INTO counters VALUES(?,?,?) ON CONFLICT(ip_key) DO UPDATE SET started=excluded.started,count=excluded.count', (key, started, count))
                db.execute('DELETE FROM counters WHERE started<?', (now - 720 * 3600,))
            return count

    @staticmethod
    def _redirect_preset(row):
        if row is None:
            raise KeyError('备用链接不存在')
        value = dict(row)
        value['check'] = json.loads(value.pop('check_result')) if row['check_result'] else {'status':'unchecked', 'platform':'网站', 'detail':'', 'checked_at':None, 'http_status':None, 'final_url':None}
        return value

    def redirect_presets(self):
        with self.connect() as db:
            return [self._redirect_preset(row) for row in db.execute('SELECT * FROM redirect_presets ORDER BY id')]

    def redirect_preset(self, preset_id):
        with self.connect() as db:
            return self._redirect_preset(db.execute('SELECT * FROM redirect_presets WHERE id=?', (preset_id,)).fetchone())

    def add_redirect_presets(self, urls, note):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = {row['url'] for row in db.execute('SELECT url FROM redirect_presets')}
            if len(existing | set(urls)) > 100:
                raise ValueError('每个域名最多保存 100 条备用链接，请先删除不用的链接')
            db.executemany('INSERT OR IGNORE INTO redirect_presets(url,note,created) VALUES(?,?,?)', [(url, note, time.time()) for url in urls])
            self._audit(db, 'b_redirect_presets_added', {'count':len(urls)})
            return [self._redirect_preset(db.execute('SELECT * FROM redirect_presets WHERE url=?', (url,)).fetchone()) for url in urls]

    def delete_redirect_preset(self, preset_id):
        with self.connect() as db:
            if not db.execute('DELETE FROM redirect_presets WHERE id=?', (preset_id,)).rowcount:
                raise KeyError('备用链接不存在')
            self._audit(db, 'b_redirect_preset_deleted', {'id':preset_id})

    def save_redirect_check(self, preset, result, commit_guard=None):
        with self.connect() as db:
            if commit_guard:
                commit_guard(db)
            if not db.execute('UPDATE redirect_presets SET check_result=? WHERE id=? AND url=?', (json.dumps(result, ensure_ascii=False), preset['id'], preset['url'])).rowcount:
                raise KeyError('备用链接已删除或变更')
            return self._redirect_preset(db.execute('SELECT * FROM redirect_presets WHERE id=?', (preset['id'],)).fetchone())

    def redirect_active(self):
        with self.connect() as db:
            row = db.execute('SELECT url,preset_id,version_id,updated FROM redirect_active WHERE id=1').fetchone()
            return dict(row) if row else None

    def redirect_split(self):
        with self.connect() as db:
            row = db.execute('SELECT enabled,mode,members,updated FROM redirect_split WHERE id=1').fetchone()
            presets = {item['id']: item['url'] for item in db.execute('SELECT id,url FROM redirect_presets')}
        if not row:
            return {'enabled': False, 'mode': 'random', 'members': [], 'updated': None}
        members = []
        for item in json.loads(row['members']):
            url = presets.get(item['preset_id'])
            if url:
                members.append({'preset_id': item['preset_id'], 'weight': item['weight'], 'url': url})
        return {'enabled': bool(row['enabled']), 'mode': row['mode'], 'members': members, 'updated': row['updated']}

    def save_redirect_split(self, enabled, mode, members):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = {row['id'] for row in db.execute('SELECT id FROM redirect_presets')}
            if any(item.preset_id not in found for item in members):
                raise ValueError('分流链接不在预设池中，请刷新后再保存')
            if enabled and not members:
                raise ValueError('开启分流时至少选择一条预设链接')
            payload = json.dumps([{'preset_id': item.preset_id, 'weight': item.weight} for item in members], ensure_ascii=False)
            db.execute('''INSERT INTO redirect_split VALUES(1,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, mode=excluded.mode, members=excluded.members, updated=excluded.updated''',
                (int(enabled), mode, payload, time.time()))
            self._audit(db, 'b_redirect_split_updated', {'enabled': enabled, 'mode': mode, 'count': len(members)})
        return self.redirect_split()

    def split_destination(self, visitor_hash):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            split = db.execute('SELECT enabled,mode,members FROM redirect_split WHERE id=1').fetchone()
            if not split or not split['enabled']:
                return None
            existing = db.execute('SELECT url FROM redirect_split_assign WHERE visitor_hash=?', (visitor_hash,)).fetchone()
            if existing:
                return existing['url']
            chosen = []
            for item in json.loads(split['members']):
                preset = db.execute('SELECT id,url FROM redirect_presets WHERE id=?', (item['preset_id'],)).fetchone()
                if preset:
                    chosen.append({'preset_id': preset['id'], 'url': preset['url'], 'weight': item['weight']})
            if not chosen:
                return None
            pick = choose_split_member(chosen, split['mode'])
            db.execute('INSERT OR IGNORE INTO redirect_split_assign(visitor_hash,preset_id,url,created) VALUES(?,?,?,?)', (visitor_hash, pick['preset_id'], pick['url'], time.time()))
            return db.execute('SELECT url FROM redirect_split_assign WHERE visitor_hash=?', (visitor_hash,)).fetchone()['url']

    def apply_redirects(self, base, preset, occurrence_ids, expected_published, commit_guard=None):
        from .redirects import replace_bundle
        if base['slot'] != 'B':
            raise KeyError('B 版本不存在')
        version, changed = replace_bundle(self.pages / base['id'], base['id'], occurrence_ids, preset['url'], self.pages, base['name'])
        try:
            with self.connect() as db:
                if commit_guard:
                    commit_guard(db)
                else:
                    db.execute('BEGIN IMMEDIATE')
                current = db.execute("SELECT version_id FROM slots WHERE slot='B'").fetchone()[0]
                if current != expected_published:
                    raise Conflict('当前 B 发布版本已变化，请重新扫描后再换链')
                saved = db.execute('SELECT url FROM redirect_presets WHERE id=?', (preset['id'],)).fetchone()
                if not saved or saved['url'] != preset['url']:
                    raise KeyError('备用链接已删除或变更')
                if version is None:
                    db.execute('INSERT INTO redirect_active VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url,preset_id=excluded.preset_id,version_id=excluded.version_id,updated=excluded.updated', (preset['url'], preset['id'], base['id'], time.time()))
                    return base | {'slot':'B'}, 0
                db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)', (version['id'], 'B', version['name'], version['created'], version['files'], version['bytes'], version['sha256']))
                db.execute("UPDATE slots SET version_id=? WHERE slot='B'", (version['id'],))
                db.execute('INSERT INTO redirect_active VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url,preset_id=excluded.preset_id,version_id=excluded.version_id,updated=excluded.updated', (preset['url'], preset['id'], version['id'], time.time()))
                self._audit(db, 'content_imported', {'slot':'B', 'version':version['id'], 'source_version':base['id'], 'files':version['files'], 'redirects_changed':changed})
                self._audit(db, 'version_published', {'slot':'B', 'before':current, 'after':version['id']})
        except Exception:
            if version is not None:
                shutil.rmtree(self.pages / version['id'])
            raise
        return version | {'slot':'B'}, changed

    @staticmethod
    def _trust_public(raw):
        blank = {'status':'unchecked', 'detail':'', 'checked_at':None}
        if not raw:
            return blank
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return blank
        status = data.get('status')
        if status not in ('trust', 'clear', 'unconfirmed'):
            return blank
        detail = {'trust':'出现信任弹窗', 'clear':'正常', 'unconfirmed':'这次没看清' if data.get('reason') != 'unconfigured' else '云手机还没接上'}[status]
        return {'status':status, 'detail':detail, 'checked_at':data.get('checked_at')}

    @classmethod
    def _whatsapp_number(cls, row):
        if row is None:
            raise KeyError('WhatsApp 号码不存在')
        value = dict(row)
        value['trust'] = cls._trust_public(value.pop('trust_result', None))
        return value

    def whatsapp_numbers(self):
        with self.connect() as db:
            return [self._whatsapp_number(row) for row in db.execute('SELECT * FROM whatsapp_numbers ORDER BY id')]

    def whatsapp_number(self, number_id):
        with self.connect() as db:
            return self._whatsapp_number(db.execute('SELECT * FROM whatsapp_numbers WHERE id=?', (number_id,)).fetchone())

    def add_whatsapp_numbers(self, phones, note, display_name=''):
        if display_name and len(phones) != 1:
            raise ValueError('写接待名时一次只填一个号码')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = {row['phone'] for row in db.execute('SELECT phone FROM whatsapp_numbers')}
            if len(existing | set(phones)) > 100:
                raise ValueError('每个域名最多保存 100 个 WhatsApp 号码，请先删除不用的号码')
            db.executemany('INSERT OR IGNORE INTO whatsapp_numbers(phone,note,created,display_name) VALUES(?,?,?,?)', [(phone, note, time.time(), display_name if len(phones) == 1 else '') for phone in phones])
            if display_name:
                db.execute('UPDATE whatsapp_numbers SET display_name=? WHERE phone=?', (display_name, phones[0]))
            self._audit(db, 'whatsapp_numbers_added', {'count':len(phones)})
            return [self._whatsapp_number(db.execute('SELECT * FROM whatsapp_numbers WHERE phone=?', (phone,)).fetchone()) for phone in phones]

    def upsert_whatsapp_name(self, phone, display_name):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id FROM whatsapp_numbers WHERE phone=?', (phone,)).fetchone()
            if row:
                db.execute('UPDATE whatsapp_numbers SET display_name=? WHERE phone=?', (display_name, phone))
            else:
                if db.execute('SELECT COUNT(*) FROM whatsapp_numbers').fetchone()[0] >= 100:
                    raise ValueError('每个域名最多保存 100 个 WhatsApp 号码，请先删除不用的号码')
                db.execute('INSERT INTO whatsapp_numbers(phone,note,created,display_name) VALUES(?,?,?,?)', (phone, '', time.time(), display_name))
            self._audit(db, 'whatsapp_numbers_added', {'phone': phone})
            saved = self._whatsapp_number(db.execute('SELECT * FROM whatsapp_numbers WHERE phone=?', (phone,)).fetchone())
        return saved

    def delete_whatsapp_number(self, number_id):
        with self.connect() as db:
            if not db.execute('DELETE FROM whatsapp_numbers WHERE id=?', (number_id,)).rowcount:
                raise KeyError('WhatsApp 号码不存在')
            self._audit(db, 'whatsapp_number_deleted', {'id':number_id})

    def save_whatsapp_trust(self, number_id, status, reason):
        if status not in ('trust', 'clear', 'unconfirmed'):
            raise ValueError('信任检测结果无效')
        payload = json.dumps({'status':status, 'reason':reason or '', 'checked_at':time.time()}, ensure_ascii=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('UPDATE whatsapp_numbers SET trust_result=? WHERE id=?', (payload, number_id)).rowcount:
                raise KeyError('WhatsApp 号码不存在')
            # Once both a trust popup and a clear screen have ever been seen, polling
            # stays on. Otherwise an auto-switch that leaves no number currently
            # 'clear' would turn polling off and stop watching the new number.
            if status in ('trust', 'clear'):
                column = 'seen_trust' if status == 'trust' else 'seen_clear'
                db.execute(f'INSERT INTO whatsapp_trust_state(id,{column}) VALUES(1,1) '
                           f'ON CONFLICT(id) DO UPDATE SET {column}=1')
            self._audit(db, 'whatsapp_trust_checked', {'id':number_id, 'status':status})
        return self.whatsapp_number(number_id)

    def whatsapp_trust_poll_enabled(self):
        with self.connect() as db:
            state = db.execute('SELECT seen_trust,seen_clear FROM whatsapp_trust_state WHERE id=1').fetchone()
            if state and state['seen_trust'] and state['seen_clear']:
                return True
            found = set()
            for row in db.execute('SELECT trust_result FROM whatsapp_numbers'):
                if not row['trust_result']:
                    continue
                try:
                    found.add(json.loads(row['trust_result']).get('status'))
                except (TypeError, ValueError):
                    continue
            return 'trust' in found and 'clear' in found

    def _reception_bundle(self, root_id, name, filename, required=False):
        from .archives import MAX_TOTAL, read_source, source_files, validate_file, write_bundle
        from .redirects import rewrite_reception_bytes
        files, total, edited, digest, changed, found = source_files(self.pages / root_id), 0, [], hashlib.sha256(), False, False
        for path, (file, _) in sorted(files.items()):
            data = read_source(file, path)
            rewritten = rewrite_reception_bytes(data, name)
            if rewritten != data:
                changed = True
            try:
                text = rewritten.decode('utf-8-sig')
            except UnicodeError:
                text = ''
            if 'reception-text' in text or '本次由助理' in text:
                found = True
            validate_file(path, rewritten)
            total += len(rewritten)
            if total > MAX_TOTAL:
                raise ValueError('源码目录超过总大小限制')
            edited.append((path, rewritten))
            digest.update(path.encode() + b'\0' + str(len(rewritten)).encode() + b'\0' + rewritten)
        if required and not found:
            raise ValueError('当前 B 页没有「本次由助理…接待」')
        if not changed:
            return None, 0
        return write_bundle(edited, filename, self.pages, digest.hexdigest()), 1

    def publish_reception(self, base, name, expected_published, commit_guard=None):
        if base['slot'] != 'B':
            raise KeyError('B 版本不存在')
        version, changed = self._reception_bundle(base['id'], name, base['name'], required=True)
        return self._commit_b_version(base, version, changed, expected_published, commit_guard, {'slot':'B', 'version': version['id'] if version else base['id'], 'reception': name, 'redirects_changed': changed})

    def publish_direct_entry(self, base, expected_published, commit_guard=None):
        """Publish the current B page with the popup skipped."""
        from .archives import MAX_TOTAL, read_source, source_files, validate_file, write_bundle
        from .redirects import read_direct_mode, set_direct_mode, strip_lead_gate
        if base['slot'] != 'B':
            raise KeyError('B 版本不存在')
        files = source_files(self.pages / base['id'])
        decoded = []
        saw_direct = False
        index_mode = None
        fallback_mode = None
        for path, (file, _) in sorted(files.items()):
            data = read_source(file, path)
            bom, text = False, None
            if Path(path).suffix.lower() in ('.html', '.htm'):
                bom = data.startswith(b'\xef\xbb\xbf')
                raw = data[3:] if bom else data
                try:
                    text = raw.decode('utf-8')
                except UnicodeError:
                    text = None
                mode = read_direct_mode(text) if text is not None else None
                if mode:
                    saw_direct = True
                    if Path(path).name.lower() in ('index.html', 'index.htm') and index_mode is None:
                        index_mode = mode
                    elif fallback_mode is None:
                        fallback_mode = mode
            decoded.append((path, data, bom, text))
        if saw_direct:
            target = 'CBA' if (index_mode or fallback_mode) == 'ABC' else 'ABC'
            total, edited, digest = 0, [], hashlib.sha256()
            changed_any = False
            for path, data, bom, text in decoded:
                if text is not None and read_direct_mode(text):
                    rewritten = set_direct_mode(text, target)
                    if rewritten != text:
                        changed_any = True
                        payload = rewritten.encode('utf-8')
                        data = (b'\xef\xbb\xbf' + payload) if bom else payload
                validate_file(path, data)
                total += len(data)
                if total > MAX_TOTAL:
                    raise ValueError('源码目录超过总大小限制')
                edited.append((path, data))
                digest.update(path.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
            if not changed_any:
                raise ValueError('开关没有变化，没有改动')
            version = write_bundle(edited, base['name'], self.pages, digest.hexdigest())
            committed, changed = self._commit_b_version(base, version, 1, expected_published, commit_guard, {'slot':'B', 'version': version['id'], 'direct_entry': True, 'redirects_changed': 1})
            return committed, changed, target.lower()
        total, edited, digest = 0, [], hashlib.sha256()
        saw_gate, saw_pool = False, False
        for path, data, bom, text in decoded:
            if text is not None:
                has_gate = bool(re.search(r'\bid\s*=\s*(["\'])lead-gate\1', text))
                if has_gate:
                    saw_gate = True
                    rewritten = strip_lead_gate(text)
                    payload = rewritten.encode('utf-8')
                    data = (b'\xef\xbb\xbf' + payload) if bom else payload
                elif 'RANDOM_POOL' in text:
                    saw_pool = True
            validate_file(path, data)
            total += len(data)
            if total > MAX_TOTAL:
                raise ValueError('源码目录超过总大小限制')
            edited.append((path, data))
            digest.update(path.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
        if not saw_gate:
            if saw_pool:
                raise ValueError('两步问卷已经拿掉，进线语已经是随机的。')
            raise ValueError('当前 B 页没有两步问卷')
        version = write_bundle(edited, base['name'], self.pages, digest.hexdigest())
        committed, changed = self._commit_b_version(base, version, 1, expected_published, commit_guard, {'slot':'B', 'version': version['id'], 'direct_entry': True, 'redirects_changed': 1})
        return committed, changed, 'gate'

    def published_direct_mode(self):
        """ABC, CBA, or None for the B page visitors are being served."""
        from .redirects import read_direct_mode
        version_id = self.slots()['B']
        if not version_id:
            return None
        root = self.pages / version_id
        if not root.is_dir():
            return None
        index_mode = None
        fallback = None
        for path in sorted(root.rglob('*')):
            if not path.is_file() or path.suffix.lower() not in ('.html', '.htm'):
                continue
            try:
                data = path.read_bytes()
                if data.startswith(b'\xef\xbb\xbf'):
                    data = data[3:]
                mode = read_direct_mode(data.decode('utf-8'))
            except (OSError, UnicodeError, ValueError):
                continue
            if not mode:
                continue
            if path.name.lower() in ('index.html', 'index.htm') and index_mode is None:
                index_mode = mode
            elif fallback is None:
                fallback = mode
        return index_mode or fallback

    def apply_whatsapp_number(self, base, number, occurrence_ids, expected_published, commit_guard=None):
        from .redirects import replace_bundle
        if base['slot'] != 'B':
            raise KeyError('B 版本不存在')
        version, changed = replace_bundle(self.pages / base['id'], base['id'], occurrence_ids, number['phone'], self.pages, base['name'])
        interim_id = None
        if number.get('display_name'):
            try:
                root_id = version['id'] if version else base['id']
                reception, reception_changed = self._reception_bundle(root_id, number['display_name'], base['name'])
            except Exception:
                if version is not None:
                    shutil.rmtree(self.pages / version['id'], ignore_errors=True)
                raise
            if reception:
                interim_id = version['id'] if version else None
                version = reception
                changed = (changed or 0) + reception_changed
        try:
            with self.connect() as db:
                if commit_guard:
                    commit_guard(db)
                else:
                    db.execute('BEGIN IMMEDIATE')
                current = db.execute("SELECT version_id FROM slots WHERE slot='B'").fetchone()[0]
                if current != expected_published:
                    raise Conflict('当前 B 发布版本已变化，请重新扫描后再换号')
                saved = db.execute('SELECT phone FROM whatsapp_numbers WHERE id=?', (number['id'],)).fetchone()
                if not saved or saved['phone'] != number['phone']:
                    raise KeyError('WhatsApp 号码已删除或变更')
                if version is None:
                    return base | {'slot':'B'}, 0
                db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)', (version['id'], 'B', version['name'], version['created'], version['files'], version['bytes'], version['sha256']))
                db.execute("UPDATE slots SET version_id=? WHERE slot='B'", (version['id'],))
                self._audit(db, 'content_imported', {'slot':'B', 'version':version['id'], 'source_version':base['id'], 'files':version['files'], 'redirects_changed':changed, 'whatsapp_number':number['phone'], 'reception': number.get('display_name') or ''})
                self._audit(db, 'version_published', {'slot':'B', 'before':current, 'after':version['id']})
        except Exception:
            if version is not None:
                shutil.rmtree(self.pages / version['id'], ignore_errors=True)
            if interim_id:
                shutil.rmtree(self.pages / interim_id, ignore_errors=True)
            raise
        if interim_id:
            shutil.rmtree(self.pages / interim_id, ignore_errors=True)
        return version | {'slot':'B'}, changed

    def _commit_b_version(self, base, version, changed, expected_published, commit_guard, detail):
        try:
            with self.connect() as db:
                if commit_guard:
                    commit_guard(db)
                else:
                    db.execute('BEGIN IMMEDIATE')
                current = db.execute("SELECT version_id FROM slots WHERE slot='B'").fetchone()[0]
                if current != expected_published:
                    raise Conflict('当前 B 发布版本已变化，请重新扫描后再换号')
                if version is None:
                    return base | {'slot':'B'}, 0
                db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)', (version['id'], 'B', version['name'], version['created'], version['files'], version['bytes'], version['sha256']))
                db.execute("UPDATE slots SET version_id=? WHERE slot='B'", (version['id'],))
                self._audit(db, 'content_imported', detail | {'source_version': base['id'], 'files': version['files']})
                self._audit(db, 'version_published', {'slot':'B', 'before':current, 'after':version['id']})
        except Exception:
            if version is not None:
                shutil.rmtree(self.pages / version['id'], ignore_errors=True)
            raise
        return version | {'slot':'B'}, changed

    def whatsapp_split(self):
        with self.connect() as db:
            row = db.execute('SELECT enabled,mode,members,updated FROM whatsapp_split WHERE id=1').fetchone()
            numbers = {item['id']: item for item in db.execute('SELECT id,phone,display_name FROM whatsapp_numbers')}
        if not row:
            return {'enabled': False, 'mode': 'random', 'members': [], 'updated': None}
        members = []
        for item in json.loads(row['members']):
            number = numbers.get(item['number_id'])
            if number and number['display_name']:
                members.append({'number_id': number['id'], 'weight': item['weight'], 'phone': number['phone'], 'display_name': number['display_name']})
        return {'enabled': bool(row['enabled']), 'mode': row['mode'], 'members': members, 'updated': row['updated']}

    def save_whatsapp_split(self, enabled, mode, members):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = {row['id']: row['display_name'] for row in db.execute('SELECT id,display_name FROM whatsapp_numbers')}
            if any(item.number_id not in found for item in members):
                raise ValueError('分流号码不在号码池中，请刷新后再保存')
            if any(not found[item.number_id] for item in members):
                raise ValueError('参与分流的号码要先写接待名')
            if enabled and not members:
                raise ValueError('开启号码分流时至少选择一个号码')
            payload = json.dumps([{'number_id': item.number_id, 'weight': item.weight} for item in members], ensure_ascii=False)
            db.execute('''INSERT INTO whatsapp_split VALUES(1,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, mode=excluded.mode, members=excluded.members, updated=excluded.updated''',
                (int(enabled), mode, payload, time.time()))
            self._audit(db, 'whatsapp_split_updated', {'enabled': enabled, 'mode': mode, 'count': len(members)})
        return self.whatsapp_split()

    def whatsapp_split_destination(self, visitor_hash):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            split = db.execute('SELECT enabled,mode,members FROM whatsapp_split WHERE id=1').fetchone()
            if not split or not split['enabled']:
                return None
            existing = db.execute('SELECT number_id,phone,display_name FROM whatsapp_split_assign WHERE visitor_hash=?', (visitor_hash,)).fetchone()
            if existing:
                current = db.execute('SELECT phone,display_name FROM whatsapp_numbers WHERE id=?', (existing['number_id'],)).fetchone()
                if current and current['display_name']:
                    return {'number_id': existing['number_id'], 'phone': current['phone'], 'display_name': current['display_name']}
                # The number was deleted or lost its reception name: drop this stale
                # assignment and pick a fresh one so a retired number leaves rotation.
                db.execute('DELETE FROM whatsapp_split_assign WHERE visitor_hash=?', (visitor_hash,))
            chosen = []
            for item in json.loads(split['members']):
                number = db.execute('SELECT id,phone,display_name FROM whatsapp_numbers WHERE id=?', (item['number_id'],)).fetchone()
                if number and number['display_name']:
                    chosen.append({'number_id': number['id'], 'phone': number['phone'], 'display_name': number['display_name'], 'weight': item['weight']})
            if not chosen:
                return None
            pick = choose_split_member(chosen, split['mode'])
            db.execute('INSERT OR REPLACE INTO whatsapp_split_assign(visitor_hash,number_id,phone,display_name,created) VALUES(?,?,?,?,?)', (visitor_hash, pick['number_id'], pick['phone'], pick['display_name'], time.time()))
            saved = db.execute('SELECT number_id,phone,display_name FROM whatsapp_split_assign WHERE visitor_hash=?', (visitor_hash,)).fetchone()
            return {'number_id': saved['number_id'], 'phone': saved['phone'], 'display_name': saved['display_name']}

    def published_tracking(self):
        from .archives import read_source, source_files
        from .tracking import read_pages
        found = {}
        slots = self.slots()
        for slot in ('A', 'B'):
            version_id = slots.get(slot)
            info = {'version_id': version_id, 'ga4': '', 'conversion': '', 'ga4_body': '', 'conversion_body': ''}
            if version_id:
                try:
                    files = source_files(self.pages / version_id)
                    pairs = [(name, read_source(path, name)) for name, (path, _) in files.items()]
                    info.update(read_pages(pairs))
                except ValueError:
                    pass
            found[slot] = info
        return found

    def apply_tracking(self, slot, ga4_body, conversion_body, expected_published):
        from .archives import MAX_TOTAL, read_source, source_files, validate_file, write_bundle
        from .tracking import rewrite_pages
        if slot not in ('A', 'B'):
            raise ValueError('只能写入 A 或 B')
        if not ga4_body and not conversion_body:
            raise ValueError('请选择要写入的 GA4 或转化代码')
        published = self.slots().get(slot)
        if not published:
            raise ValueError('这个位置还没有发布页面')
        if expected_published and expected_published != published:
            raise Conflict('当前发布版本已变化，请刷新后再写入')
        base = self.version(published)
        if not base or base['slot'] != slot:
            raise ValueError('版本不存在或槽位不符')
        files = source_files(self.pages / published)
        pairs = [(name, read_source(path, name)) for name, (path, _) in sorted(files.items())]
        edited, changed = rewrite_pages(pairs, ga4_body, conversion_body)
        if not changed:
            raise ValueError('页面里的代码已经是这一份')
        total, digest = 0, hashlib.sha256()
        checked = []
        for name, data in edited:
            validate_file(name, data)
            total += len(data)
            if total > MAX_TOTAL:
                raise ValueError('源码目录超过总大小限制')
            checked.append((name, data))
            digest.update(name.encode('utf-8') + b'\x00' + str(len(data)).encode() + b'\x00' + data)
        version = write_bundle(checked, base['name'], self.pages, digest.hexdigest())
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT version_id FROM slots WHERE slot=?', (slot,)).fetchone()[0]
                if current != published:
                    raise Conflict('当前发布版本已变化，请刷新后再写入')
                db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)', (version['id'], slot, version['name'], version['created'], version['files'], version['bytes'], version['sha256']))
                db.execute('UPDATE slots SET version_id=? WHERE slot=?', (version['id'], slot))
                self._audit(db, 'content_imported', {'slot': slot, 'version': version['id'], 'source_version': published, 'files': version['files'], 'tracking': True})
                self._audit(db, 'version_published', {'slot': slot, 'before': current, 'after': version['id']})
        except Exception:
            shutil.rmtree(self.pages / version['id'], ignore_errors=True)
            raise
        return version | {'slot': slot}

    def reset_counts(self):
        with self.connect() as db:
            db.execute('DELETE FROM counters')
            self._audit(db, 'counters_reset', {'scope': 'all IP visit counters; link allocation counts unchanged'})

    def clear_logs(self):
        with self.connect() as db:
            deleted = db.execute('DELETE FROM events').rowcount
            self._audit(db, 'logs_cleared', {'deleted': deleted})
        return deleted

    def clear_logs_unless(self, keep):
        """Delete visit rows in this site only. keep(ip, country) uses the same display rule as the log list."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id, ip, country FROM events').fetchall()
            remove = [row['id'] for row in rows if not keep(row['ip'], row['country'])]
            deleted = 0
            for start in range(0, len(remove), 500):
                chunk = remove[start:start + 500]
                deleted += db.execute(f"DELETE FROM events WHERE id IN ({','.join('?' * len(chunk))})", chunk).rowcount
            self._audit(db, 'logs_foreign_cleared', {'deleted': deleted, 'kept': len(rows) - deleted})
        return {'deleted': deleted, 'kept': len(rows) - deleted}

    def add_links(self, slot, urls):
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO links(slot,url) VALUES(?,?)', [(slot, url) for url in urls])
            self._audit(db, 'links_added', {'slot': slot, 'count': len(urls)})

    def delete_link(self, link_id):
        with self.connect() as db:
            if not db.execute('DELETE FROM links WHERE id=?', (link_id,)).rowcount:
                raise ValueError('链接不存在')
            self._audit(db, 'link_deleted', {'id': link_id})

    def links(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM links ORDER BY id')]

    def choose_link(self, slot, strategy, consume=True):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM links WHERE slot=? ORDER BY id', (slot,)).fetchall()
            if not rows:
                return None
            if strategy == 'random':
                row = secrets.choice(rows)
            elif strategy == 'equal':
                row = min(rows, key=lambda item: (item['hits'], item['id']))
            else:
                cursor = db.execute('SELECT last_id FROM rotation WHERE slot=?', (slot,)).fetchone()
                last = cursor[0] if cursor else 0
                row = next((item for item in rows if item['id'] > last), rows[0])
            if consume:
                db.execute('UPDATE links SET hits=hits+1 WHERE id=?', (row['id'],))
                db.execute('INSERT INTO rotation VALUES(?,?) ON CONFLICT(slot) DO UPDATE SET last_id=excluded.last_id', (slot, row['id']))
            return dict(row)

    def event(self, ip, country, decision, path, mode):
        exact = str(ipaddress.ip_address(ip))
        with self.connect() as db:
            details = decision.get('device_details')
            db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode,device_details) VALUES(?,?,?,?,?,?,?,?,?)', (time.time(), exact, country, decision['device'], decision['slot'], decision['reason'], path[:512], mode, json.dumps(details, ensure_ascii=False) if details else None))
            db.execute('DELETE FROM events WHERE created<? OR id <= (SELECT MAX(id)-10000 FROM events)', (time.time() - 30 * 86400,))

    def recent_count(self, seconds=60):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM events WHERE created>=?', (time.time() - seconds,)).fetchone()[0]

    def logs(self, days=7, slot='', page=1, page_size=25):
        where = 'created>=?'
        args = [time.time() - days * 86400]
        if slot:
            where += ' AND slot=?'
            args.append(slot)
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM events WHERE ' + where, args).fetchone()[0]
            rows = db.execute('SELECT * FROM events WHERE ' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?', args + [page_size, (page - 1) * page_size]).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                item['device_details'] = json.loads(item['device_details']) if item['device_details'] else None
            return {'items': items, 'total': total, 'page': page, 'pages': max(1, (total + page_size - 1) // page_size)}

    def stats(self):
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) total,COALESCE(SUM(slot='B'),0) allowed,COALESCE(SUM(slot='A'),0) blocked,COALESCE(SUM(created>=?),0) today FROM events WHERE created>=?", (midnight, time.time() - 30 * 86400)).fetchone()
            return dict(row)

    def audit(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')]

    def record_access(self, ua, ch_ua, ch_platform, ch_platform_version, ch_mobile, referer, query, wa_number, cf_cache_status, country, city, status, duration_ms, path):
        """One row per visitor request. A failure here must not break the page."""
        with self.connect() as db:
            db.execute('''INSERT INTO access_log(created,ua,ch_ua,ch_platform,ch_platform_version,ch_mobile,referer,query,wa_number,cf_cache_status,country,city,status,duration_ms,path)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                time.time(), ua, ch_ua, ch_platform, ch_platform_version, ch_mobile, referer, query, wa_number, cf_cache_status, country, city, status, duration_ms, path))
            latest = db.execute('SELECT MAX(id) FROM access_log').fetchone()[0]
            if latest and latest % 200 == 0:
                db.execute('DELETE FROM access_log WHERE created<?', (time.time() - 90 * 86400,))

    def record_client_event(self, body):
        with self.connect() as db:
            db.execute('INSERT INTO client_events(created,body) VALUES(?,?)', (time.time(), body))
            latest = db.execute('SELECT MAX(id) FROM client_events').fetchone()[0]
            if latest and latest % 200 == 0:
                db.execute('DELETE FROM client_events WHERE created<?', (time.time() - 90 * 86400,))

    def access_report(self, days=7, country=None):
        """Plain-text weekly export. Days are Hong Kong dates. Counts are requests."""
        since = time.time() - days * 86400
        where, args = 'created>=?', [since]
        if country:
            where += ' AND country=?'
            args.append(country)
        with self.connect() as db:
            ua_rows = db.execute(f'SELECT ua, COUNT(*) n FROM access_log WHERE {where} GROUP BY ua ORDER BY n DESC, ua', args).fetchall()
            cross = db.execute(f'''SELECT date(created, 'unixepoch', '+8 hours') day,
                    CASE WHEN instr(ua, 'wv)')>0 THEN 1 ELSE 0 END wv,
                    CASE WHEN (instr(lower(ua), 'iphone')>0 OR instr(lower(ua), 'ipad')>0 OR instr(lower(ua), 'ipod')>0) AND instr(ua, 'Safari/')=0 THEN 1 ELSE 0 END ios_no_safari,
                    CASE WHEN instr(lower(query), 'gclid=')>0 THEN 1 ELSE 0 END gclid,
                    CASE WHEN instr(lower(query), 'placement=')>0 THEN 1 ELSE 0 END placement,
                    COUNT(*) n
                FROM access_log WHERE {where} GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5''', args).fetchall()
            events = db.execute('SELECT created, body FROM client_events WHERE created>=? ORDER BY id', (since,)).fetchall()
        scope = country or '全部'
        lines = [
            '访问日志周报',
            f'范围：最近 {days} 天，日期按香港时间',
            f'国家筛选：{scope}',
            '只含香港访客。条数按请求计，页面上的每个资源各算一条。边缘缓存命中不到源站，不会出现在这里。',
            'cf-cache-status 只记录请求头里实际带来的值；Cloudflare 默认不把这个响应头回传源站。',
            '',
            '# 1 去重后的完整 User-Agent，按条数降序',
            '条数\tUser-Agent',
        ]
        lines.extend(f'{row["n"]}\t{row["ua"]}' for row in ua_rows)
        lines.extend([
            '',
            '# 2 按日 × 含 wv) × iOS且不含 Safari/ × 带 gclid × 带 placement',
            '日期\t含wv)\tiOS且不含Safari/\t带gclid\t带placement\t请求数',
        ])
        lines.extend(f'{row["day"]}\t{row["wv"]}\t{row["ios_no_safari"]}\t{row["gclid"]}\t{row["placement"]}\t{row["n"]}' for row in cross)
        lines.extend([
            '',
            '# 3 事件端点收到的全部记录（不受国家筛选，每行一条收到的 JSON）',
        ])
        for row in events:
            stamp = datetime.fromtimestamp(row['created'], timezone(timedelta(hours=8))).isoformat(timespec='seconds')
            lines.append(f'{stamp}\t{row["body"]}')
        return '\n'.join(lines) + '\n'

    def recent_access(self, since, limit):
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM access_log WHERE created>=?', (since,)).fetchone()[0]
            rows = db.execute('''SELECT created,ua,ch_ua,ch_platform,ch_platform_version,ch_mobile,referer,query,wa_number,cf_cache_status,country,city,status,duration_ms,path
                FROM access_log WHERE created>=? ORDER BY created DESC, id DESC LIMIT ?''', (since, limit)).fetchall()
        return total, [dict(row) for row in rows]

    def recent_client_events(self, since, limit):
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM client_events WHERE created>=?', (since,)).fetchone()[0]
            rows = db.execute('SELECT created, body FROM client_events WHERE created>=? ORDER BY created DESC, id DESC LIMIT ?', (since, limit)).fetchall()
        return total, [dict(row) for row in rows]
