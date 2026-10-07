import json
import sqlite3
import threading
import uuid

import pytest

from ablab.auth import Auth, password_hash
from ablab.store import Conflict, Store


PASSWORD = 'a-long-test-password-123'
NEW_PASSWORD = 'another-long-password-456'


def signed_in(auth, username, password=PASSWORD, ip='203.0.113.9'):
    token, status = auth.login(username, password, ip)
    assert status == 200
    return token


def test_legacy_migration_preserves_admin_secret_and_invalidates_old_sessions(tmp_path):
    store = Store(tmp_path)
    salt = '0123456789abcdef0123456789abcdef'
    encoded = f'scrypt${salt}${password_hash(PASSWORD, salt)}'
    with store.connect() as db:
        db.execute('CREATE TABLE admin_account (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)')
        db.execute('CREATE TABLE admin_sessions (token_hash TEXT PRIMARY KEY, username TEXT, expires REAL)')
        db.execute('INSERT INTO admin_account VALUES (1,?,?)', ('admin888', encoded))
        db.execute('INSERT INTO admin_sessions VALUES (?,?,?)', (Auth.key('old-token'), 'admin888', 9999999999))
    auth = Auth(store)
    assert auth.ready()
    admin = auth.admin_account()
    assert {key: admin[key] for key in ('id', 'username', 'role', 'enabled')} == {
        'id': 'admin', 'username': 'admin888', 'role': 'admin', 'enabled': True}
    assert auth.session('old-token') is None
    token = signed_in(auth, 'admin888')
    restarted = Auth(store)
    assert restarted.admin_account() == admin
    assert restarted.session(token)['account_id'] == 'admin'
    with store.connect() as db:
        assert db.execute('SELECT password_hash FROM accounts WHERE id=?', ('admin',)).fetchone()[0] == encoded
        assert db.execute('SELECT COUNT(*) FROM admin_sessions').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['username', 'password', 'both'])
def test_reupgrade_syncs_password_set_by_legacy_cli_and_revokes_only_admin(tmp_path, change):
    auth = Auth(Store(tmp_path))
    auth.set_password('admin888', PASSWORD)
    agent = auth.create_agent('agent', PASSWORD)
    admin_token = signed_in(auth, 'admin888')
    agent_token = signed_in(auth, 'agent')
    created = auth.admin_account()['created']
    username = 'legacy_owner' if change != 'password' else 'admin888'
    password = NEW_PASSWORD if change != 'username' else PASSWORD
    with auth.store.connect() as db:
        epoch = db.execute("SELECT auth_epoch FROM accounts WHERE id='admin'").fetchone()[0]
        encoded = db.execute("SELECT password_hash FROM accounts WHERE id='admin'").fetchone()[0]
    if change != 'username':
        salt = '0123456789abcdef0123456789abcdef'
        encoded = f'scrypt${salt}${password_hash(password, salt)}'
    with auth.store.connect() as db:
        # The rolled back configure.py writes only these legacy tables.
        db.execute('INSERT OR REPLACE INTO admin_account VALUES (1,?,?)', (username, encoded))
        db.execute('INSERT INTO admin_sessions VALUES (?,?,?)', (Auth.key('legacy-token'), username, 9999999999))
    upgraded = Auth(auth.store)
    assert upgraded.admin_account() == {
        'id': 'admin', 'username': username, 'role': 'admin', 'enabled': True, 'created': created}
    assert upgraded.session(admin_token) is None
    assert upgraded.session('legacy-token') is None
    assert upgraded.session(agent_token)['account_id'] == agent['id']
    assert upgraded.login('admin888', PASSWORD, '203.0.113.10') == (None, 401)
    fresh_token = signed_in(upgraded, username, password)
    with auth.store.connect() as db:
        assert db.execute("SELECT auth_epoch FROM accounts WHERE id='admin'").fetchone()[0] == epoch + 1
    assert Auth(auth.store).session(fresh_token)['account_id'] == 'admin'


def test_reupgrade_legacy_username_collision_rolls_back_entire_migration(tmp_path):
    auth = Auth(Store(tmp_path))
    auth.set_password('admin888', PASSWORD)
    agent = auth.create_agent('agent', PASSWORD)
    admin_token = signed_in(auth, 'admin888')
    agent_token = signed_in(auth, 'agent')
    with auth.store.connect() as db:
        db.execute("UPDATE admin_account SET username='agent'")
        db.execute('INSERT INTO admin_sessions VALUES (?,?,?)', (Auth.key('legacy-token'), 'agent', 9999999999))
        before = [dict(row) for row in db.execute('SELECT * FROM accounts ORDER BY id')]
    with pytest.raises(Conflict):
        Auth(auth.store)
    with auth.store.connect() as db:
        assert [dict(row) for row in db.execute('SELECT * FROM accounts ORDER BY id')] == before
        assert db.execute('SELECT COUNT(*) FROM admin_sessions').fetchone()[0] == 1
    assert auth.session(admin_token)['account_id'] == 'admin'
    assert auth.session(agent_token)['account_id'] == agent['id']


def test_agent_public_records_sessions_and_account_local_revocation(tmp_path):
    auth = Auth(Store(tmp_path))
    assert not auth.ready()
    assert auth.admin_account() is None
    auth.set_password('admin888', PASSWORD)
    agent = auth.create_agent('agent_one', PASSWORD)
    other = auth.create_agent('agent_two', PASSWORD)
    assert uuid.UUID(agent['id'])
    assert agent['id'] != 'admin' and agent['role'] == 'agent' and agent['enabled'] is True
    assert set(agent) == {'id', 'username', 'role', 'enabled', 'created'}
    assert auth.get_account(agent['id']) == agent
    assert {row['id'] for row in auth.list_accounts()} == {'admin', agent['id'], other['id']}
    admin_token = signed_in(auth, 'admin888')
    agent_token = signed_in(auth, 'agent_one')
    other_token = signed_in(auth, 'agent_two')
    session = auth.session(agent_token)
    assert set(session) == {'username', 'account_id', 'role', 'csrf'}
    assert session['account_id'] == agent['id'] and session['role'] == 'agent'
    auth.set_enabled(agent['id'], False)
    assert auth.session(agent_token) is None
    assert auth.login('agent_one', PASSWORD, '203.0.113.10') == (None, 401)
    assert auth.session(admin_token) and auth.session(other_token)
    restarted = Auth(auth.store)
    assert restarted.get_account(agent['id']) == {**agent, 'enabled': False}
    assert restarted.session(other_token)['account_id'] == other['id']
    auth.set_enabled(agent['id'], True)
    assert auth.session(agent_token) is None
    replacement = signed_in(auth, 'agent_one')
    auth.reset_agent_password(agent['id'], NEW_PASSWORD)
    assert auth.session(replacement) is None
    assert auth.login('agent_one', PASSWORD, '203.0.113.11') == (None, 401)
    fresh = signed_in(auth, 'agent_one', NEW_PASSWORD)
    assert auth.session(admin_token) and auth.session(other_token)
    auth.set_password('renamed_admin', NEW_PASSWORD)
    assert auth.session(admin_token) is None
    assert auth.session(fresh) and auth.session(other_token)
    assert auth.admin_account()['id'] == 'admin'
    auth.logout(fresh)
    assert auth.session(fresh) is None
    with auth.store.connect() as db:
        details = [json.loads(row[0]) for row in db.execute('SELECT detail FROM audit')]
    assert PASSWORD not in json.dumps(details) and NEW_PASSWORD not in json.dumps(details)


def test_account_validation_and_username_collision_are_atomic(tmp_path):
    auth = Auth(Store(tmp_path))
    auth.set_password('admin888', PASSWORD)
    agent = auth.create_agent('agent', PASSWORD)
    for username in ('admin888', 'agent'):
        with pytest.raises(Conflict):
            auth.create_agent(username, PASSWORD)
    with pytest.raises(Conflict):
        auth.set_password('agent', NEW_PASSWORD)
    assert auth.admin_account()['username'] == 'admin888'
    assert auth.session(signed_in(auth, 'admin888'))['account_id'] == 'admin'
    for username in ('', 'with space', 'a' * 65, None):
        with pytest.raises(ValueError):
            auth.create_agent(username, PASSWORD)
    for password in ('short', 'a' * 257, None):
        with pytest.raises(ValueError):
            auth.create_agent('valid', password)
    with pytest.raises(ValueError):
        auth.set_enabled('admin', False)
    with pytest.raises(ValueError):
        auth.reset_agent_password('admin', NEW_PASSWORD)
    for enabled in (0, 1, 'false', None):
        with pytest.raises(ValueError):
            auth.set_enabled(agent['id'], enabled)
    for operation in (auth.get_account, lambda account_id: auth.set_enabled(account_id, False),
                      lambda account_id: auth.reset_agent_password(account_id, NEW_PASSWORD)):
        with pytest.raises(KeyError):
            operation('missing')


@pytest.mark.parametrize('change', ['disable', 'disable_enable', 'password'])
def test_login_during_account_change_cannot_issue_session(tmp_path, monkeypatch, change):
    auth = Auth(Store(tmp_path))
    agent = auth.create_agent('agent', PASSWORD)
    hashing, resume = threading.Event(), threading.Event()
    result = []

    def paused_hash(password, salt):
        hashing.set()
        assert resume.wait(10)
        return password_hash(password, salt)

    monkeypatch.setattr('ablab.auth.password_hash', paused_hash)
    thread = threading.Thread(target=lambda: result.append(auth.login('agent', PASSWORD, '203.0.113.9')))
    thread.start()
    assert hashing.wait(10)
    monkeypatch.setattr('ablab.auth.password_hash', password_hash)
    try:
        if change == 'password':
            auth.reset_agent_password(agent['id'], NEW_PASSWORD)
        else:
            auth.set_enabled(agent['id'], False)
            if change == 'disable_enable':
                auth.set_enabled(agent['id'], True)
    finally:
        resume.set()
        thread.join(10)
    assert not thread.is_alive()
    assert result == [(None, 401)]


def test_session_reads_current_enabled_and_login_rate_limit(tmp_path):
    auth = Auth(Store(tmp_path))
    agent = auth.create_agent('agent', PASSWORD)
    token = signed_in(auth, 'agent')
    with auth.store.connect() as db:
        db.execute('UPDATE accounts SET enabled=0 WHERE id=?', (agent['id'],))
    assert auth.session(token) is None
    for _ in range(5):
        assert auth.login('missing', PASSWORD, '203.0.113.12') == (None, 401)
    assert auth.login('missing', PASSWORD, '203.0.113.12') == (None, 429)


def test_session_secret_hash_csrf_and_expiry(tmp_path):
    auth = Auth(Store(tmp_path))
    agent = auth.create_agent('agent', PASSWORD)
    token = signed_in(auth, 'agent')
    session = auth.session(token)
    assert len(session['csrf']) == 64 and session['csrf'] != token
    assert auth.session(token)['csrf'] == session['csrf']
    with auth.store.connect() as db:
        encoded = db.execute('SELECT password_hash FROM accounts WHERE id=?', (agent['id'],)).fetchone()[0]
        stored = db.execute('SELECT token_hash FROM account_sessions').fetchone()[0]
        assert encoded.startswith('scrypt$') and PASSWORD not in encoded
        assert stored != token
        db.execute('UPDATE account_sessions SET expires=0')
    assert auth.session(token) is None


def test_global_login_rate_limit_covers_different_ips(tmp_path):
    auth = Auth(Store(tmp_path))
    for suffix in range(30):
        assert auth.login('missing', PASSWORD, f'203.0.113.{suffix}') == (None, 401)
    assert auth.login('missing', PASSWORD, '203.0.113.31') == (None, 429)


@pytest.mark.parametrize('disabled', [False, True])
def test_unknown_and_disabled_login_wait_for_password_hash(tmp_path, monkeypatch, disabled):
    auth = Auth(Store(tmp_path))
    if disabled:
        agent = auth.create_agent('agent', PASSWORD)
        auth.set_enabled(agent['id'], False)
    hashing, resume = threading.Event(), threading.Event()
    result = []

    def paused_hash(password, salt):
        hashing.set()
        assert resume.wait(10)
        return password_hash(password, salt)

    monkeypatch.setattr('ablab.auth.password_hash', paused_hash)
    thread = threading.Thread(target=lambda: result.append(auth.login('agent', PASSWORD, '203.0.113.9')))
    thread.start()
    try:
        assert hashing.wait(1), 'Username existence must not bypass password work'
        assert not result
    finally:
        resume.set()
        thread.join(10)
    assert result == [(None, 401)]


def test_observer_signs_in_and_admin_revokes_without_showing_the_password(tmp_path):
    auth = Auth(Store(tmp_path))
    auth.set_password('admin888', PASSWORD)
    watcher = auth.create_observer('watcher', PASSWORD)
    assert set(watcher) == {'id', 'username', 'role', 'enabled', 'created'}
    assert watcher['role'] == 'observer' and watcher['enabled'] is True
    token = signed_in(auth, 'watcher')
    assert auth.session(token)['role'] == 'observer'
    auth.set_enabled(watcher['id'], False)
    assert auth.session(token) is None
    assert auth.login('watcher', PASSWORD, '203.0.113.20') == (None, 401)
    auth.set_enabled(watcher['id'], True)
    replacement = signed_in(auth, 'watcher')
    auth.reset_agent_password(watcher['id'], NEW_PASSWORD)
    assert auth.session(replacement) is None
    fresh = signed_in(auth, 'watcher', NEW_PASSWORD)
    assert auth.session(fresh)['account_id'] == watcher['id']
    with pytest.raises(Conflict):
        auth.create_agent('watcher', PASSWORD)
    with auth.store.connect() as db:
        actions = [row[0] for row in db.execute('SELECT action FROM audit')]
        detail = ' '.join(row[0] for row in db.execute('SELECT detail FROM audit'))
    assert 'observer_created' in actions and 'observer_login' in actions and 'observer_password_reset' in actions
    assert PASSWORD not in detail and NEW_PASSWORD not in detail


def test_old_account_check_keeps_rows_and_sessions_when_observer_is_added(tmp_path):
    store = Store(tmp_path)
    with store.connect() as db:
        db.execute('''CREATE TABLE accounts (
            id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            created REAL NOT NULL, auth_epoch INTEGER NOT NULL DEFAULT 0,
            CHECK((id='admin' AND role='admin') OR (id!='admin' AND role='agent')))''')
        db.execute('''CREATE TABLE account_sessions (
            token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
            expires REAL NOT NULL)''')
        db.execute('CREATE INDEX account_session_owner ON account_sessions(account_id)')
        db.execute("INSERT INTO accounts(id,username,password_hash,role,enabled,created,auth_epoch) VALUES ('admin','boss','hash','admin',1,1,3)")
        db.execute("INSERT INTO accounts(id,username,password_hash,role,enabled,created,auth_epoch) VALUES ('agentid','agent','hash','agent',1,2,4)")
        db.execute("INSERT INTO account_sessions(token_hash,account_id,expires) VALUES ('kept','agentid',9999999999)")
    auth = Auth(store)
    watcher = auth.create_observer('watcher', PASSWORD)
    assert watcher['role'] == 'observer'
    assert auth.get_account('agentid')['username'] == 'agent'
    assert auth.get_account('admin')['username'] == 'boss'
    with store.connect() as db:
        assert db.execute("SELECT token_hash FROM account_sessions WHERE account_id='agentid'").fetchone()[0] == 'kept'
        assert 'observer' in db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO accounts(id,username,password_hash,role,created) VALUES ('nope','nope','h','guest',1)")
    assert Auth(store).get_account(watcher['id'])['role'] == 'observer'


@pytest.mark.parametrize('username,password', [
    ('agent', 'short'), ('agent', 'x' * 257), ('agent', None),
    (None, PASSWORD), ('a' * 65, PASSWORD), ('bad name', PASSWORD),
])
def test_malformed_login_does_not_do_expensive_password_work(tmp_path, monkeypatch, username, password):
    auth = Auth(Store(tmp_path))
    auth.create_agent('agent', PASSWORD)

    def rejected_hash(*args):
        pytest.fail('Malformed credentials must be rejected before scrypt')

    monkeypatch.setattr('ablab.auth.password_hash', rejected_hash)
    assert auth.login(username, password, '203.0.113.9') == (None, 401)
