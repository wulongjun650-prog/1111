from datetime import datetime, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ablab.auth import Auth
from ablab.analytics import summarize
from ablab.settings import Deployment
from ablab.sites import Registry
from ablab.store import Store
from ablab.web import create_admin


NOW = datetime(2026, 10, 2, 4, 30, tzinfo=timezone.utc).timestamp()  # 12:30 at UTC+8


def client(tmp_path):
    app = create_admin(tmp_path)
    return TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 10000))


def event(store, created, reason, slot='A', country=None, ip='203.0.113.0/24'):
    with store.connect() as db:
        db.execute(
            'INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)',
            (created, ip, country, 'mobile', slot, reason, '/', 'RULES'),
        )


def test_outcomes_follow_reasons_independently_of_displayed_slot(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    store = Store(tmp_path)
    for reason, slot, country in [
        ('allowed', 'A', 'US'), ('pass', 'B', 'US'), ('whitelist', 'A', None),
        ('protection_off', 'A', 'CN'), ('blacklist', 'A', 'CN'),
        ('device', 'A', None), ('manual', 'A', 'US'), ('surprise', 'B', None),
    ]:
        event(store, NOW - 60, reason, slot, country)
    response = client(tmp_path).get('/api/analytics?period=today')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['summary'] == {'total': 8, 'allowed': 4, 'blocked': 2, 'other': 2, 'rate': 50.0, 'countries': 2, 'a': 6, 'b': 2}
    assert result['trend'][12] == {'label': '12:00', 'total': 8, 'allowed': 4, 'blocked': 2, 'other': 2}
    assert len(result['trend']) == 24
    assert result['countries'] == [
        {'code': 'US', 'total': 3, 'allowed': 2, 'blocked': 0, 'other': 1},
        {'code': None, 'total': 3, 'allowed': 1, 'blocked': 1, 'other': 1},
        {'code': 'CN', 'total': 2, 'allowed': 1, 'blocked': 1, 'other': 0},
    ]
    assert result['reasons'] == [{'reason': 'blacklist', 'count': 1}, {'reason': 'device', 'count': 1}]
    assert result['retention'] == {'days': 30, 'max_events_per_site': 10000}
    assert len(result['recent']) == 8
    assert result['recent'][0]['ip'] == '203.0.113.0/24'
    assert 'secret' not in str(result)


def test_current_and_all_scopes_validate_site_and_keep_site_data_separate(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    registry = Registry(tmp_path)
    first = registry.add('one.example.com')
    second = registry.add('two.example.com')
    event(registry.store('default'), NOW - 10, 'allowed', 'B')
    event(registry.store(first['id']), NOW - 20, 'blacklist', 'A')
    event(registry.store(second['id']), NOW - 30, 'manual', 'B')
    admin = client(tmp_path)
    path = f"/api/sites/{first['id']}/analytics?period=7d"
    current = admin.get(path).json()
    assert current['summary']['total'] == 1
    assert current['domains'] == [{'id': first['id'], 'domain': first['domain'], 'total': 1, 'allowed': 0, 'blocked': 1, 'other': 0}]
    assert current['recent'][0]['site_id'] == first['id']
    assert current['recent'][0]['domain'] == first['domain']
    all_sites = admin.get(path + '&scope=all').json()
    assert all_sites['summary']['total'] == 3
    assert all_sites['summary']['allowed'] == 1
    assert all_sites['summary']['blocked'] == 1
    assert all_sites['summary']['other'] == 1
    assert [row['id'] for row in all_sites['domains']] == ['default', first['id'], second['id']]
    assert [row['site_id'] for row in all_sites['recent']] == ['default', first['id'], second['id']]
    assert admin.get('/api/sites/unknown/analytics?scope=all').status_code == 404
    assert admin.get('/api/analytics?scope=current').json()['summary']['total'] == 1


def test_all_scope_does_not_initialize_unvisited_site(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    registry = Registry(tmp_path)
    site = registry.add('quiet.example.com')
    database = tmp_path / 'sites' / site['id'] / 'app.db'
    assert not database.exists()
    result = client(tmp_path).get('/api/analytics?scope=all').json()
    assert result['domains'][1] == {'id': site['id'], 'domain': site['domain'], 'total': 0, 'allowed': 0, 'blocked': 0, 'other': 0}
    assert not database.exists()


def test_recent_keeps_eight_newest_across_sites(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    registry = Registry(tmp_path)
    site = registry.add('busy.example.com')
    for index in range(9):
        event(registry.store('default'), NOW - 100 + index, 'allowed', 'B')
    event(registry.store(site['id']), NOW - 1, 'blacklist')
    result = client(tmp_path).get('/api/analytics?scope=all').json()
    assert result['summary']['total'] == 10
    assert len(result['recent']) == 8
    assert result['recent'][0]['site_id'] == site['id']
    assert [row['created'] for row in result['recent']] == [NOW - 1] + [NOW - 92 - index for index in range(7)]


def test_analytics_reads_existing_databases_without_write_permission(tmp_path, monkeypatch):
    registry = Registry(tmp_path)
    event(registry.store('default'), NOW - 1, 'allowed', 'B')
    original = sqlite3.connect

    def read_only_connection(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in {
            sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE,
        } else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', read_only_connection)
    assert summarize(registry, 'default', 'today', 'current', 480, NOW)['summary']['total'] == 1


def test_malformed_stored_countries_are_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    store = Store(tmp_path)
    event(store, NOW - 1, 'allowed', 'B', 'US')
    event(store, NOW - 1, 'allowed', 'B', 'XXX')
    event(store, NOW - 1, 'allowed', 'B', 'ZZ')
    result = client(tmp_path).get('/api/analytics').json()
    assert result['summary']['countries'] == 1
    assert result['countries'] == [
        {'code': None, 'total': 2, 'allowed': 2, 'blocked': 0, 'other': 0},
        {'code': 'US', 'total': 1, 'allowed': 1, 'blocked': 0, 'other': 0},
    ]


def test_all_scope_rejects_corrupt_registry_site_id(tmp_path):
    registry = Registry(tmp_path)
    with registry.connect() as db:
        db.execute("INSERT INTO sites(id,domain,stage,created) VALUES('../outside','bad.example.com','active',?)", (NOW,))
    with pytest.raises(ValueError, match='站点ID格式错误'):
        summarize(registry, 'default', '7d', 'all', 480, NOW)


def test_all_scope_reads_legacy_site_without_device_details_migration(tmp_path):
    registry = Registry(tmp_path)
    site = registry.add('legacy.example.com')
    directory = tmp_path / 'sites' / site['id']
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / 'app.db') as db:
        db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, created REAL NOT NULL, ip TEXT NOT NULL, country TEXT, device TEXT NOT NULL, slot TEXT NOT NULL, reason TEXT NOT NULL, path TEXT NOT NULL, mode TEXT NOT NULL)')
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)',
                   (NOW - 1, '203.0.113.0/24', 'US', 'mobile', 'B', 'allowed', '/', 'RULES'))
    result = summarize(registry, 'default', 'today', 'all', 480, NOW)
    assert result['summary']['total'] == 1
    assert result['recent'][0]['device_details'] is None
    with sqlite3.connect(directory / 'app.db') as db:
        assert 'device_details' not in {row[1] for row in db.execute('PRAGMA table_info(events)')}


def test_calendar_windows_offset_and_future_exclusion(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    store = Store(tmp_path)
    # UTC+8: today begins Oct 1 at 16:00 UTC; yesterday begins Sep 30 at 16:00.
    for when in [
        datetime(2026, 9, 30, 15, 59, tzinfo=timezone.utc).timestamp(),
        datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc).timestamp(),
        datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc).timestamp(),
        datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc).timestamp(),
        NOW,
        NOW + 1,
    ]:
        event(store, when, 'allowed', 'B')
    admin = client(tmp_path)
    today = admin.get('/api/analytics?period=today&tz_offset=480').json()
    assert today['summary']['total'] == 2
    assert today['start'] == '2026-10-01T16:00:00+00:00'
    assert today['end'] == '2026-10-02T04:30:00+00:00'
    assert today['trend'][0]['total'] == 1
    assert today['trend'][12]['total'] == 1
    yesterday = admin.get('/api/analytics?period=yesterday&tz_offset=480').json()
    assert yesterday['summary']['total'] == 2
    assert yesterday['start'] == '2026-09-30T16:00:00+00:00'
    assert yesterday['end'] == '2026-10-01T16:00:00+00:00'
    seven = admin.get('/api/analytics?period=7d&tz_offset=480').json()
    assert len(seven['trend']) == 7
    assert seven['trend'][-1]['label'] == '2026-10-02'
    assert seven['summary']['total'] == 5
    utc = admin.get('/api/analytics?period=today&tz_offset=0').json()
    assert utc['summary']['total'] == 1
    assert utc['trend'][4]['total'] == 1


def test_cloudflare_japan_joins_the_hong_kong_total(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    store = Store(tmp_path)
    event(store, NOW - 60, 'allowed', 'B', 'JP', '172.64.215.0/24')
    event(store, NOW - 50, 'allowed', 'B', 'HK', '1.32.192.0/24')
    event(store, NOW - 40, 'allowed', 'B', 'JP', '203.0.113.0/24')
    result = client(tmp_path).get('/api/analytics?period=today').json()
    totals = {row['code']: row['total'] for row in result['countries']}
    assert totals['HK'] == 2
    assert totals['JP'] == 1
    shown = {item['ip']: item['country'] for item in result['recent']}
    assert shown['172.64.215.0/24'] == 'HK'
    assert shown['1.32.192.0/24'] == 'HK'
    assert shown['203.0.113.0/24'] == 'JP'


def test_invalid_query_and_production_auth(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: NOW)
    admin = client(tmp_path)
    for query in ['period=week', 'scope=everywhere', 'tz_offset=841', 'tz_offset=-841', 'tz_offset=1.5']:
        assert admin.get('/api/analytics?' + query).status_code == 422
    tmp_path = tmp_path / 'production'
    settings = Deployment(admin_origin='https://admin.lab.example', target_origin='https://lab.example')
    Auth(Store(tmp_path)).set_password('owner', 'a-long-test-password-123')
    app = create_admin(tmp_path, deployment=settings)
    production = TestClient(app, base_url=settings.admin_origin, client=('127.0.0.1', 32100))
    production.headers.update({'X-Real-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https', 'Origin': settings.admin_origin, 'X-CSRF-Token': app.state.csrf})
    assert production.get('/api/analytics').status_code == 401
    assert production.get('/api/sites/default/analytics').status_code == 401
    assert production.post('/api/login', json={'username': 'owner', 'password': 'a-long-test-password-123'}).status_code == 200
    assert production.get('/api/analytics').status_code == 200
