import hashlib
import hmac
import ipaddress
import json
import re
from contextlib import contextmanager
from datetime import datetime
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
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, created REAL NOT NULL, ip TEXT NOT NULL, country TEXT, device TEXT NOT NULL, slot TEXT NOT NULL, reason TEXT NOT NULL, path TEXT NOT NULL, mode TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_created ON events(created);
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

    def delete_version(self, version_id):
        """Hard-delete one unpublished B version, its files, and a stale active-link row."""
        target = self._version_directory(version_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('SELECT id, slot, name FROM versions WHERE id=?', (version_id,)).fetchone()
            if not version or version['slot'] != 'B':
                raise ValueError('只能删除未发布的 B 版本')
            published = db.execute("SELECT version_id FROM slots WHERE slot='B'").fetchone()[0]
            if published == version_id:
                raise ValueError('不能删除当前发布的 B 版本')
            active = db.execute('SELECT version_id FROM redirect_active WHERE id=1').fetchone()
            cleared_active = bool(active and active['version_id'] == version_id)
            if cleared_active:
                db.execute('DELETE FROM redirect_active WHERE id=1')
            if not db.execute('DELETE FROM versions WHERE id=? AND slot=?', (version_id, 'B')).rowcount:
                raise ValueError('只能删除未发布的 B 版本')
            self._audit(db, 'b_version_deleted', {'version': version_id, 'name': version['name'], 'cleared_active': cleared_active})
        if target.exists():
            shutil.rmtree(target)
        return version_id

    def delete_unpublished_b_versions(self):
        with self.connect() as db:
            published = db.execute("SELECT version_id FROM slots WHERE slot='B'").fetchone()[0]
            ids = [row['id'] for row in db.execute("SELECT id FROM versions WHERE slot='B' AND (? IS NULL OR id != ?) ORDER BY created", (published, published))]
        deleted = []
        for version_id in ids:
            try:
                self.delete_version(version_id)
            except ValueError:
                continue
            deleted.append(version_id)
        return deleted

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
            self._audit(db, 'whatsapp_trust_checked', {'id':number_id, 'status':status})
        return self.whatsapp_number(number_id)

    def whatsapp_trust_poll_enabled(self):
        with self.connect() as db:
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
                return {'number_id': existing['number_id'], 'phone': existing['phone'], 'display_name': existing['display_name']}
            chosen = []
            for item in json.loads(split['members']):
                number = db.execute('SELECT id,phone,display_name FROM whatsapp_numbers WHERE id=?', (item['number_id'],)).fetchone()
                if number and number['display_name']:
                    chosen.append({'number_id': number['id'], 'phone': number['phone'], 'display_name': number['display_name'], 'weight': item['weight']})
            if not chosen:
                return None
            pick = choose_split_member(chosen, split['mode'])
            db.execute('INSERT OR IGNORE INTO whatsapp_split_assign(visitor_hash,number_id,phone,display_name,created) VALUES(?,?,?,?,?)', (visitor_hash, pick['number_id'], pick['phone'], pick['display_name'], time.time()))
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
