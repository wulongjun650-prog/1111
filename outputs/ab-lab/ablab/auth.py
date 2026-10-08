"""Administrator and agent accounts with revocable server-side sessions."""
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid

from .store import Conflict

COOKIE = '__Host-ab_session'
SESSION_SECONDS = 8 * 3600
# One expensive password operation at a time keeps memory bounded on small VPSs.
PASSWORD_LOCK = threading.Lock()
ACCOUNTS_SQL = '''CREATE TABLE accounts (
    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    created REAL NOT NULL, auth_epoch INTEGER NOT NULL DEFAULT 0,
    CHECK((id='admin' AND role='admin') OR (id!='admin' AND role IN ('agent','observer'))))'''
SESSIONS_SQL = '''CREATE TABLE account_sessions (
    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
    expires REAL NOT NULL)'''


def password_hash(password, salt):
    with PASSWORD_LOCK:
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=131072,
                              r=8, p=1, maxmem=256 * 1024 * 1024, dklen=32).hex()


class Auth:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS admin_account (
                id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS ' + ACCOUNTS_SQL.removeprefix('CREATE TABLE '))
            db.execute('CREATE TABLE IF NOT EXISTS ' + SESSIONS_SQL.removeprefix('CREATE TABLE '))
            db.execute('CREATE INDEX IF NOT EXISTS account_session_owner ON account_sessions(account_id)')
            self._allow_observer(db)
            db.execute('CREATE TABLE IF NOT EXISTS login_attempts (ip_key TEXT NOT NULL, created REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS login_time ON login_attempts(created)')
            db.execute('''INSERT INTO accounts(id,username,password_hash,role,created)
                SELECT 'admin',username,password_hash,'admin',? FROM admin_account WHERE id=1
                AND NOT EXISTS(SELECT 1 FROM accounts WHERE id='admin')''', (time.time(),))
            legacy = db.execute('SELECT username,password_hash FROM admin_account WHERE id=1').fetchone()
            current = db.execute("SELECT username,password_hash FROM accounts WHERE id='admin'").fetchone()
            # A rolled back CLI can change only the legacy table; reconcile on reupgrade.
            if legacy and current and tuple(legacy) != tuple(current):
                try:
                    db.execute('''UPDATE accounts SET username=?,password_hash=?,enabled=1,
                        auth_epoch=auth_epoch+1 WHERE id='admin' ''', tuple(legacy))
                except sqlite3.IntegrityError:
                    raise Conflict('旧版管理员账号与代理账号重名，请先修改旧版管理员账号') from None
                db.execute("DELETE FROM account_sessions WHERE account_id='admin'")
                self.store._audit(db, 'admin_password_migrated', {'username': legacy['username']})
            # Legacy tokens have no account identity and must never be accepted.
            db.execute('DELETE FROM admin_sessions')

    @staticmethod
    def _allow_observer(db):
        """SQLite cannot alter a CHECK. Rebuild accounts so existing rows can sit beside observers."""
        current = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()
        if current is None or 'observer' in current['sql']:
            return
        db.execute('ALTER TABLE accounts RENAME TO accounts_legacy_roles')
        db.execute(ACCOUNTS_SQL)
        db.execute('''INSERT INTO accounts(id,username,password_hash,role,enabled,created,auth_epoch)
            SELECT id,username,password_hash,role,enabled,created,auth_epoch FROM accounts_legacy_roles''')
        db.execute('''CREATE TABLE account_sessions_next (
            token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
            expires REAL NOT NULL)''')
        db.execute('''INSERT INTO account_sessions_next(token_hash,account_id,expires)
            SELECT token_hash,account_id,expires FROM account_sessions''')
        db.execute('DROP TABLE account_sessions')
        db.execute('DROP TABLE accounts_legacy_roles')
        db.execute('ALTER TABLE account_sessions_next RENAME TO account_sessions')
        db.execute('CREATE INDEX IF NOT EXISTS account_session_owner ON account_sessions(account_id)')

    def ready(self):
        return self.admin_account() is not None

    @staticmethod
    def _public(row):
        account = {key: row[key] for key in ('id', 'username', 'role', 'enabled', 'created')}
        account['enabled'] = bool(account['enabled'])
        return account

    def admin_account(self):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE id='admin'").fetchone()
        return self._public(row) if row else None

    def list_accounts(self):
        with self.store.connect() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM accounts ORDER BY role,created,id")]

    def get_account(self, account_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        return self._public(row)

    @staticmethod
    def _username(username):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username):
            raise ValueError('账号只允许 1–64 位字母、数字、_.@-')

    @staticmethod
    def _encode(password):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError('密码长度必须为 12–256 字符')
        salt = secrets.token_hex(16)
        return f'scrypt${salt}${password_hash(password, salt)}'

    def set_password(self, username, password):
        self._username(username)
        encoded = self._encode(password)
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('''INSERT INTO accounts(id,username,password_hash,role,created)
                    VALUES ('admin',?,?,'admin',?) ON CONFLICT(id) DO UPDATE SET
                    username=excluded.username,password_hash=excluded.password_hash,enabled=1,
                    auth_epoch=accounts.auth_epoch+1''', (username, encoded, time.time()))
                db.execute('INSERT OR REPLACE INTO admin_account VALUES (1,?,?)', (username, encoded))
                db.execute("DELETE FROM account_sessions WHERE account_id='admin'")
                db.execute('DELETE FROM admin_sessions')
                db.execute('DELETE FROM login_attempts')
                self.store._audit(db, 'admin_password_set', {'username': username})
        except sqlite3.IntegrityError:
            raise Conflict('账号已存在') from None

    def create_agent(self, username, password):
        return self._create_account(username, password, 'agent', 'agent_created')

    def create_observer(self, username, password):
        return self._create_account(username, password, 'observer', 'observer_created')

    def _create_account(self, username, password, role, action):
        self._username(username)
        encoded = self._encode(password)
        account_id = uuid.uuid4().hex
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('''INSERT INTO accounts(id,username,password_hash,role,created)
                    VALUES (?,?,?,?,?)''', (account_id, username, encoded, role, time.time()))
                self.store._audit(db, action, {'account_id': account_id, 'username': username})
                return self._public(db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone())
        except sqlite3.IntegrityError:
            raise Conflict('账号已存在') from None

    @staticmethod
    def _managed(db, account_id):
        row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        if row['role'] not in ('agent', 'observer'):
            raise ValueError('只能修改代理或观察号')
        return row

    def set_enabled(self, account_id, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('enabled 必须为布尔值')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._managed(db, account_id)
            db.execute('UPDATE accounts SET enabled=?,auth_epoch=auth_epoch+1 WHERE id=?', (enabled, account_id))
            db.execute('DELETE FROM account_sessions WHERE account_id=?', (account_id,))
            self.store._audit(db, 'account_enabled', {'account_id': account_id, 'enabled': enabled})
            return self._public(db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone())

    def reset_agent_password(self, account_id, password):
        with self.store.connect() as db:
            role = self._managed(db, account_id)['role']
        encoded = self._encode(password)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._managed(db, account_id)
            db.execute('UPDATE accounts SET password_hash=?,auth_epoch=auth_epoch+1 WHERE id=?', (encoded, account_id))
            db.execute('DELETE FROM account_sessions WHERE account_id=?', (account_id,))
            action = 'observer_password_reset' if role == 'observer' else 'agent_password_reset'
            self.store._audit(db, action, {'account_id': account_id})

    def login(self, username, password, ip):
        now = time.time()
        del ip
        if (not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username)
                or not isinstance(password, str) or not 12 <= len(password) <= 256):
            return None, 401
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE username=? AND enabled=1', (username,)).fetchone()
        # Unknown and disabled accounts do the same work as a wrong password.
        encoded = row['password_hash'] if row else f'scrypt${"0" * 32}${"0" * 64}'
        _, salt, expected = encoded.split('$')
        valid = hmac.compare_digest(password_hash(password, salt), expected)
        if not row or not valid:
            return None, 401
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Epoch detects disable/enable even when the password is unchanged.
            current = db.execute('SELECT enabled,password_hash,auth_epoch FROM accounts WHERE id=?', (row['id'],)).fetchone()
            if not current or not current['enabled'] or current['password_hash'] != row['password_hash'] or current['auth_epoch'] != row['auth_epoch']:
                return None, 401
            db.execute('DELETE FROM account_sessions WHERE expires<?', (now,))
            db.execute('INSERT INTO account_sessions VALUES (?,?,?)', (self.key(token), row['id'], now + SESSION_SECONDS))
            action = {'admin': 'admin_login', 'agent': 'agent_login', 'observer': 'observer_login'}[row['role']]
            self.store._audit(db, action, {'username': username, 'account_id': row['id']})
        return token, 200

    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token):
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        with self.store.connect() as db:
            row = db.execute('''SELECT a.id,a.username,a.role FROM account_sessions s JOIN accounts a
                             ON a.id=s.account_id WHERE s.token_hash=? AND s.expires>? AND a.enabled=1''',
                             (self.key(token), time.time())).fetchone()
        if row is None:
            return None
        return {'username': row['username'], 'account_id': row['id'], 'role': row['role'],
                'csrf': hmac.new(self.store.secret, ('csrf:' + token).encode(), hashlib.sha256).hexdigest()}

    def logout(self, token):
        with self.store.connect() as db:
            db.execute('DELETE FROM account_sessions WHERE token_hash=?', (self.key(token),))
            self.store._audit(db, 'admin_logout', {})
