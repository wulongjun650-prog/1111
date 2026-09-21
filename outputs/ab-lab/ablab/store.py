import hashlib
import hmac
import ipaddress
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import secrets
import sqlite3
import time

from .models import Config


class Conflict(ValueError):
    pass


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
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, created REAL NOT NULL, ip TEXT NOT NULL, country TEXT, device TEXT NOT NULL, slot TEXT NOT NULL, reason TEXT NOT NULL, path TEXT NOT NULL, mode TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_created ON events(created);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, created REAL NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(events)')}
            if 'device_details' not in columns:
                db.execute('ALTER TABLE events ADD COLUMN device_details TEXT')
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

    def reset_counts(self):
        with self.connect() as db:
            db.execute('DELETE FROM counters')
            self._audit(db, 'counters_reset', {'scope': 'all IP visit counters; link allocation counts unchanged'})

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
        address = ipaddress.ip_address(ip)
        prefix = 24 if address.version == 4 else 48
        masked = str(ipaddress.ip_network(f'{address}/{prefix}', strict=False))
        with self.connect() as db:
            details = decision.get('device_details')
            db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode,device_details) VALUES(?,?,?,?,?,?,?,?,?)', (time.time(), masked, country, decision['device'], decision['slot'], decision['reason'], path[:512], mode, json.dumps(details, ensure_ascii=False) if details else None))
            db.execute('DELETE FROM events WHERE created<? OR id <= (SELECT MAX(id)-10000 FROM events)', (time.time() - 30 * 86400,))

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
