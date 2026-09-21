"""Single administrator, salted scrypt passwords and revocable server-side sessions."""
import hashlib
import hmac
import re
import secrets
import threading
import time

COOKIE = '__Host-ab_session'
SESSION_SECONDS = 8 * 3600
# One expensive password operation at a time keeps memory bounded on small VPSs.
PASSWORD_LOCK = threading.Lock()


def password_hash(password, salt):
    with PASSWORD_LOCK:
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=131072,
                              r=8, p=1, maxmem=256 * 1024 * 1024, dklen=32).hex()


class Auth:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS admin_account (
                    id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts (ip_key TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS login_time ON login_attempts(created);
            ''')

    def ready(self):
        with self.store.connect() as db:
            return db.execute('SELECT 1 FROM admin_account').fetchone() is not None

    def set_password(self, username, password):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username):
            raise ValueError('账号只允许 1–64 位字母、数字、_.@-')
        if not 12 <= len(password) <= 256:
            raise ValueError('密码长度必须为 12–256 字符')
        salt = secrets.token_hex(16)
        encoded = f'scrypt${salt}${password_hash(password, salt)}'
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO admin_account VALUES (1,?,?)', (username, encoded))
            db.execute('DELETE FROM admin_sessions')
            db.execute('DELETE FROM login_attempts')
            self.store._audit(db, 'admin_password_set', {'username': username})

    def login(self, username, password, ip):
        now = time.time()
        ip_key = hmac.new(self.store.secret, ip.encode(), hashlib.sha256).hexdigest()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM login_attempts WHERE created<?', (now - 900,))
            attempts = db.execute('SELECT COUNT(*) FROM login_attempts WHERE ip_key=?', (ip_key,)).fetchone()[0]
            total = db.execute('SELECT COUNT(*) FROM login_attempts WHERE created>?', (now - 60,)).fetchone()[0]
            if attempts >= 5 or total >= 30:
                return None, 429
            db.execute('INSERT INTO login_attempts VALUES (?,?)', (ip_key, now))
            row = db.execute('SELECT username,password_hash FROM admin_account WHERE id=1').fetchone()
        if not row or not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
            return None, 401
        _, salt, expected = row['password_hash'].split('$')
        valid = hmac.compare_digest(password_hash(password, salt), expected)
        if not valid or username != row['username']:
            return None, 401
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Password may have been changed while the slow hash was running.
            current = db.execute('SELECT password_hash FROM admin_account WHERE id=1').fetchone()
            if not current or current[0] != row['password_hash']:
                return None, 401
            db.execute('DELETE FROM admin_sessions WHERE expires<?', (now,))
            db.execute('DELETE FROM login_attempts WHERE ip_key=?', (ip_key,))
            db.execute('INSERT INTO admin_sessions VALUES (?,?,?)', (self.key(token), username, now + SESSION_SECONDS))
            self.store._audit(db, 'admin_login', {'username': username})
        return token, 200

    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.store.connect() as db:
            row = db.execute('SELECT username FROM admin_sessions WHERE token_hash=? AND expires>?',
                             (self.key(token), time.time())).fetchone()
        if row is None:
            return None
        return {'username': row['username'], 'csrf': hmac.new(self.store.secret, ('csrf:' + token).encode(), hashlib.sha256).hexdigest()}

    def logout(self, token):
        with self.store.connect() as db:
            db.execute('DELETE FROM admin_sessions WHERE token_hash=?', (self.key(token),))
            self.store._audit(db, 'admin_logout', {})
