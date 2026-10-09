"""Registered domains only. Business stores never use a hostname as a path."""
from contextlib import contextmanager
import ipaddress
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .store import Conflict, Store


def normalize_domain(value):
    if not isinstance(value, str) or value != value.strip() or len(value) > 253:
        raise ValueError('请输入完整域名，不含协议、端口、路径或空白')
    try:
        domain = value.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise ValueError('域名格式不正确') from None
    labels = domain.split('.')
    if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels):
        raise ValueError('域名格式不正确')
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        if not labels[-1].isdigit() and labels[-1] not in ('localhost', 'local', 'internal'):
            return domain
    raise ValueError('需要公网域名，不能填写 IP 或本地主机名')


class Registry:
    def __init__(self, data_dir, default_domain='', reserved=(), registry_dir=None):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.reserved = set(reserved)
        folder = Path(registry_dir).resolve() if registry_dir else self.root
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / 'registry.db'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sites(
                    id TEXT PRIMARY KEY, domain TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    stage TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                    next_attempt REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    panel_id INTEGER, managed_path TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS site_events(
                    id INTEGER PRIMARY KEY, site_id TEXT NOT NULL, created REAL NOT NULL,
                    stage TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tracking_snippets(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, label TEXT NOT NULL,
                    body TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                    UNIQUE(kind, label));
            ''')
            # Serialize migration across admin and target startup. SQLite backup
            # includes WAL; only a complete backup is installed at the final path.
            db.execute('BEGIN IMMEDIATE')
            if 'note' not in {row['name'] for row in db.execute('PRAGMA table_info(sites)')}:
                db.execute("ALTER TABLE sites ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            if 'owner_id' not in {row['name'] for row in db.execute('PRAGMA table_info(sites)')}:
                db.execute("ALTER TABLE sites ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'")
            if 'owner_epoch' not in {row['name'] for row in db.execute('PRAGMA table_info(sites)')}:
                db.execute('ALTER TABLE sites ADD COLUMN owner_epoch INTEGER NOT NULL DEFAULT 0')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(sites)')}
            for name, definition in (
                ('cf_zone_id', "TEXT NOT NULL DEFAULT ''"),
                ('cf_nameservers', "TEXT NOT NULL DEFAULT ''"),
                ('cf_status', "TEXT NOT NULL DEFAULT ''"),
                ('cf_detail', "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    db.execute(f'ALTER TABLE sites ADD COLUMN {name} {definition}')
            if not db.execute("SELECT 1 FROM sites WHERE id='default'").fetchone() and (self.root / 'app.db').exists():
                backup = self.root / 'backups' / 'pre-multidomain.db'
                backup.parent.mkdir(exist_ok=True)
                if not backup.exists():
                    temporary = backup.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                    with sqlite3.connect(self.root / 'app.db') as source:
                        dest = sqlite3.connect(temporary)
                        try:
                            source.backup(dest)
                        finally:
                            dest.close()
                    temporary.replace(backup)
            db.execute("INSERT OR IGNORE INTO sites(id,domain,stage,created) VALUES('default',?,'legacy',?)", (default_domain, time.time()))
            old = db.execute("SELECT domain FROM sites WHERE id='default'").fetchone()[0]
            if default_domain and old != default_domain:
                raise ValueError('原站域名与登记表不一致，请先备份并核对配置，不能自动迁移到其他域名')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self, owner_id=None):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sites WHERE (? IS NULL OR owner_id=?) ORDER BY CASE id WHEN 'default' THEN 0 ELSE 1 END,created", (owner_id, owner_id))]

    def get(self, site_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sites WHERE id=?', (site_id,)).fetchone()
        if row is None:
            raise KeyError(site_id)
        return dict(row)

    def for_host(self, host):
        try:
            domain = normalize_domain(host)
        except ValueError:
            return None
        with self.connect() as db:
            row = db.execute('SELECT * FROM sites WHERE domain=? AND enabled=1', (domain,)).fetchone()
        return dict(row) if row else None

    def add(self, domain, owner_id='admin'):
        domain = normalize_domain(domain)
        if domain in self.reserved:
            raise ValueError('不能将后台管理域名登记为访客站点')
        site_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM sites').fetchone()[0] >= 200:
                raise ValueError('最多登记200个站点')
            try:
                db.execute('INSERT INTO sites(id,domain,stage,created,owner_id) VALUES(?,?,?,?,?)', (site_id, domain, 'unconfigured', time.time(), owner_id))
            except sqlite3.IntegrityError:
                raise Conflict('域名已经登记') from None
            self._event(db, site_id, 'unconfigured', '域名已登记；等待服务器配置接入服务')
        return self.get(site_id)

    def remove(self, site_id):
        """Drop the registry row and this site's data directory. Root-owned files are queued separately."""
        if site_id == 'default':
            raise ValueError('原有站点不能删除')
        if not isinstance(site_id, str) or not re.fullmatch(r'[a-f0-9]{32}', site_id):
            raise ValueError('站点ID格式错误')
        directory = self.root / 'sites' / site_id
        if directory.is_symlink():
            raise ValueError('站点目录不能是符号链接')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sites WHERE id=?', (site_id,)).fetchone()
            if row is None:
                raise KeyError(site_id)
            snapshot = dict(row)
            tables = {item[0] for item in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            db.execute('DELETE FROM site_events WHERE site_id=?', (site_id,))
            if 'google_reputation' in tables:
                db.execute('DELETE FROM google_reputation WHERE site_id=?', (site_id,))
            if 'google_reputation_locks' in tables:
                db.execute('DELETE FROM google_reputation_locks WHERE site_id=?', (site_id,))
            db.execute('DELETE FROM sites WHERE id=?', (site_id,))
        snapshot['_wipe_failed'] = False
        try:
            from .site_erasure import wipe_site_data
            wipe_site_data(self.root, site_id)
        except (OSError, ValueError):
            snapshot['_wipe_failed'] = True
        return snapshot

    def assign_owner(self, site_id, owner_id):
        if not isinstance(owner_id, str) or not re.fullmatch(r'admin|[a-f0-9]{32}', owner_id):
            raise ValueError('账号ID格式错误')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            site = db.execute('SELECT owner_id,stage FROM sites WHERE id=?', (site_id,)).fetchone()
            if site is None:
                raise KeyError(site_id)
            if site['owner_id'] != owner_id:
                db.execute('UPDATE sites SET owner_id=?,owner_epoch=owner_epoch+1 WHERE id=?', (owner_id, site_id))
                self._event(db, site_id, site['stage'], '管理员调整域名归属')
        return self.get(site_id)

    def store(self, site_id):
        site = self.get(site_id)
        if site['id'] == 'default':
            return Store(self.root)
        if not re.fullmatch('[a-f0-9]{32}', site['id']):
            raise ValueError('站点ID格式错误')
        directory = self.root / 'sites' / site['id']
        if not directory.resolve().is_relative_to(self.root):
            raise ValueError('站点目录越界')
        return Store(directory)

    def _event(self, db, site_id, stage, detail):
        db.execute('INSERT INTO site_events(site_id,created,stage,detail) VALUES(?,?,?,?)', (site_id, time.time(), stage, detail))
        db.execute('DELETE FROM site_events WHERE id <= (SELECT MAX(id)-5000 FROM site_events)')

    def events(self, site_id):
        self.get(site_id)
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT created,stage,detail FROM site_events WHERE site_id=? ORDER BY id DESC LIMIT 100', (site_id,))]

    def set_cloudflare(self, site_id, zone_id, nameservers, status, detail):
        if not isinstance(zone_id, str) or (zone_id and not re.fullmatch(r'[a-f0-9]{32}', zone_id)):
            raise ValueError('Cloudflare 站点记录无效')
        if not isinstance(nameservers, str) or len(nameservers) > 300:
            raise ValueError('Cloudflare NS 记录无效')
        if nameservers and any(not re.fullmatch(r'[a-z0-9.-]{1,253}', part) for part in nameservers.split(',')):
            raise ValueError('Cloudflare NS 记录无效')
        if status not in ('', 'pending', 'active', 'failed'):
            raise ValueError('Cloudflare 状态无效')
        if not isinstance(detail, str) or len(detail) > 400:
            raise ValueError('Cloudflare 说明过长')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            site = db.execute('SELECT stage,cf_zone_id,cf_nameservers,cf_status,cf_detail FROM sites WHERE id=?', (site_id,)).fetchone()
            if site is None:
                raise KeyError(site_id)
            if (site['cf_zone_id'], site['cf_nameservers'], site['cf_status'], site['cf_detail']) != (zone_id, nameservers, status, detail):
                db.execute('UPDATE sites SET cf_zone_id=?,cf_nameservers=?,cf_status=?,cf_detail=? WHERE id=?', (zone_id, nameservers, status, detail, site_id))
                self._event(db, site_id, site['stage'], (detail or 'Cloudflare 记录已更新')[:180])
        return self.get(site_id)

    def clear_cloudflare(self, site_id, detail):
        if not isinstance(detail, str) or not detail.strip() or len(detail) > 400:
            raise ValueError('Cloudflare 说明无效')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            site = db.execute('SELECT stage FROM sites WHERE id=?', (site_id,)).fetchone()
            if site is None:
                raise KeyError(site_id)
            if site['stage'] in ('active', 'legacy', 'paused'):
                stage = site['stage']
                db.execute(
                    "UPDATE sites SET cf_zone_id='',cf_nameservers='',cf_status='',cf_detail='',generation=generation+1 WHERE id=?",
                    (site_id,))
            else:
                stage = 'waiting_dns'
                db.execute(
                    "UPDATE sites SET cf_zone_id='',cf_nameservers='',cf_status='',cf_detail='',stage='waiting_dns',next_attempt=0,error=?,generation=generation+1 WHERE id=?",
                    (detail, site_id))
            self._event(db, site_id, stage, detail[:180])
        return self.get(site_id)

    def tracking_snippets(self):
        with self.connect() as db:
            return [self._tracking_public(row) for row in db.execute('SELECT * FROM tracking_snippets ORDER BY id')]

    def tracking_snippet(self, snippet_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tracking_snippets WHERE id=?', (snippet_id,)).fetchone()
        if row is None:
            raise ValueError('保存的代码不存在')
        return dict(row)

    def add_tracking_snippet(self, kind, label, body, note):
        if kind not in ('ga4', 'conversion'):
            raise ValueError('代码类型无效')
        note = note.strip()
        if len(note) > 300:
            raise ValueError('备注最多 300 字')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id FROM tracking_snippets WHERE kind=? AND label=?', (kind, label)).fetchone()
            if row:
                db.execute('UPDATE tracking_snippets SET body=?,note=? WHERE id=?', (body, note, row['id']))
                snippet_id = row['id']
            else:
                count = db.execute('SELECT COUNT(*) AS n FROM tracking_snippets WHERE kind=?', (kind,)).fetchone()['n']
                if count >= 100:
                    raise ValueError('最多保存 100 条，请先删掉不用的')
                cursor = db.execute(
                    'INSERT INTO tracking_snippets(kind,label,body,note,created) VALUES(?,?,?,?,?)',
                    (kind, label, body, note, time.time()))
                snippet_id = cursor.lastrowid
            return self._tracking_public(db.execute('SELECT * FROM tracking_snippets WHERE id=?', (snippet_id,)).fetchone())

    def delete_tracking_snippet(self, snippet_id):
        with self.connect() as db:
            if not db.execute('DELETE FROM tracking_snippets WHERE id=?', (snippet_id,)).rowcount:
                raise ValueError('保存的代码不存在')

    @staticmethod
    def _tracking_public(row):
        return {'id': row['id'], 'kind': row['kind'], 'label': row['label'], 'note': row['note'], 'created': row['created']}

    def set_note(self, site_id, note):
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError('备注须为不超过200字的文本')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            site = db.execute('SELECT note,stage FROM sites WHERE id=?', (site_id,)).fetchone()
            if site is None:
                raise KeyError(site_id)
            if site['note'] != note:
                db.execute('UPDATE sites SET note=? WHERE id=?', (note, site_id))
                self._event(db, site_id, site['stage'], '备注已更新')
        return self.get(site_id)

    def set_availability(self, site_id, action):
        if action not in ('online', 'offline'):
            raise ValueError('站点状态无效')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            site = db.execute('SELECT enabled,stage,next_attempt FROM sites WHERE id=?', (site_id,)).fetchone()
            if site is None:
                raise KeyError(site_id)
            if action == 'offline':
                if site['enabled']:
                    db.execute('UPDATE sites SET enabled=0,generation=generation+1 WHERE id=?', (site_id,))
                    self._event(db, site_id, site['stage'], '用户下线')
            elif site['stage'] in ('active', 'legacy'):
                if not site['enabled']:
                    db.execute('UPDATE sites SET enabled=1,generation=generation+1 WHERE id=?', (site_id,))
                    self._event(db, site_id, site['stage'], '用户上线')
            elif not site['enabled'] or site['stage'] != 'waiting_dns' or site['next_attempt']:
                db.execute("UPDATE sites SET enabled=1,stage='waiting_dns',next_attempt=0,generation=generation+1 WHERE id=?", (site_id,))
                self._event(db, site_id, 'waiting_dns', '用户开启接入')
        return self.get(site_id)

    def control(self, site_id, action):
        self.get(site_id)
        if site_id == 'default':
            raise ValueError('原站由已有部署管理，不在自动接入任务范围内')
        enabled = action == 'retry'
        stage = 'waiting_dns' if enabled else 'paused'
        with self.connect() as db:
            db.execute('UPDATE sites SET enabled=?,stage=?,error=?,next_attempt=0,generation=generation+1 WHERE id=?', (enabled, stage, '', site_id))
            self._event(db, site_id, stage, '用户请求重试' if enabled else '用户暂停接入和访问；不删除面板站点或数据')
        return self.get(site_id)

    def transition(self, site_id, generation, stage, detail='', delay=0, panel_id=None, managed_path=None):
        """CAS prevents an in-flight worker from undoing an administrator pause."""
        with self.connect() as db:
            cursor = db.execute('UPDATE sites SET stage=?,error=?,next_attempt=?,attempts=attempts+1,panel_id=COALESCE(?,panel_id),managed_path=COALESCE(?,managed_path) WHERE id=? AND generation=? AND enabled=1',
                                (stage, detail, time.time() + delay, panel_id, managed_path, site_id, generation))
            if cursor.rowcount:
                self._event(db, site_id, stage, detail)
            return bool(cursor.rowcount)
