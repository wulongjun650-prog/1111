import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
from ablab import web
from ablab.models import Config, Visitor
from ablab.rules import decide
from ablab.store import Store


def test_devices_have_details_without_guessing_iphone_model():
    ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 Version/17.1 Mobile/15E148 Safari/604.1'
    result = decide(Config(), Visitor(ip='1.1.1.1', ua=ua))
    details = result.get('device_details', {})
    assert details.get('device') == 'iPhone'
    assert details.get('os') == 'iOS 17.1'
    assert details.get('browser') == 'Safari 17.1'
    assert details.get('model') == ''
    assert result['device'] == 'mobile'


def test_android_ua_model_and_reduced_ua():
    real = 'Mozilla/5.0 (Linux; Android 13; SM-S918B Build/TP1A) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'
    result = decide(Config(), Visitor(ip='1.1.1.1', ua=real)).get('device_details', {})
    assert result.get('model') == 'SM-S918B'
    assert result.get('brand') == 'Samsung'
    assert result.get('browser') == 'Chrome 120.0.0.0'
    reduced = real.replace('SM-S918B Build/TP1A', 'K')
    assert decide(Config(), Visitor(ip='1.1.1.1', ua=reduced)).get('device_details', {}).get('model') == ''


def test_edge_is_not_chrome_and_unknown_stays_unknown():
    result = decide(Config(), Visitor(ip='1.1.1.1', ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.2210.91'))
    assert result.get('device_details', {}).get('browser') == 'Edge 120.0.2210.91'
    assert result.get('device_details', {}).get('os') == 'Windows 10 / 11'
    unknown = decide(Config(), Visitor(ip='1.1.1.1')).get('device_details', {})
    assert unknown.get('device') == '手机'
    assert unknown.get('os') == 'iOS 18 以上'
    assert unknown.get('browser') == 'Safari'
    assert '未知' not in unknown.get('device') + unknown.get('os') + unknown.get('browser')


def test_legacy_log_migration_is_repeatable_and_new_details_persist(tmp_path):
    store = Store(tmp_path)
    decision = decide(Config(), Visitor(ip='1.1.1.1', ua='iPhone CPU iPhone OS 17_1'))
    store.event('1.1.1.1', 'KR', decision, '/', 'RULES')
    row = Store(tmp_path).logs()['items'][0]
    assert row.get('device_details', {}).get('device') == 'iPhone'
    assert row['ip'] == '1.1.1.0/24'
    assert row['country'] == 'KR'


def test_legacy_table_gets_nullable_details_without_fabrication(tmp_path):
    db = sqlite3.connect(tmp_path / 'app.db')
    db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, created REAL NOT NULL, ip TEXT NOT NULL, country TEXT, device TEXT NOT NULL, slot TEXT NOT NULL, reason TEXT NOT NULL, path TEXT NOT NULL, mode TEXT NOT NULL)')
    db.execute("INSERT INTO events VALUES(1, strftime('%s','now'), '1.1.1.0/24', NULL, 'mobile','B','allowed','/','RULES')")
    db.commit(); db.close()
    row = Store(tmp_path).logs()['items'][0]
    assert 'device_details' in row and row['device_details'] is None
    assert row['country'] is None
    assert Store(tmp_path).logs()['total'] == 1


def test_geo_status_exposes_reason_without_private_server_paths(monkeypatch):
    monkeypatch.setenv('AB_GEOIP_PATH', '/private/not-found.mmdb')
    status = getattr(web, 'geo_status', lambda: {})()
    assert status.get('ready') is False
    assert status.get('reason') == 'unavailable'
    assert '/private/' not in str(status)


def test_geo_lookup_rejects_nonpublic_and_invalid_codes(monkeypatch):
    class Reader:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, ip): return {'country': {'iso_code': 'xx'}}
        def metadata(self): return SimpleNamespace(database_type='DBIP-Country-Lite', build_epoch=1788220800)
    monkeypatch.setenv('AB_GEOIP_PATH', 'fixture.mmdb')
    monkeypatch.setattr(web.maxminddb, 'open_database', lambda _: Reader())
    assert web.geo_country('127.0.0.1') is None
    assert web.geo_country('8.8.8.8') is None


def test_http_logs_and_csv_include_device_details(tmp_path):
    target = TestClient(web.create_target(tmp_path), base_url='http://127.0.0.1:8766', client=('127.0.0.1', 1))
    target.get('/', headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) Version/17.1 Mobile Safari/604.1'})
    admin = TestClient(web.create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 1))
    row = admin.get('/api/logs').json()['items'][0]
    assert row.get('device_details', {}).get('os') == 'iOS 17.1'
    assert 'iOS 17.1' in admin.get('/api/logs.csv').text


def test_bundled_database_and_explicit_missing_override(monkeypatch):
    monkeypatch.delenv('AB_GEOIP_PATH', raising=False)
    assert web.geo_status()['ready'] is True
    assert web.geo_status()['bundled'] is True
    assert web.geo_country('8.8.8.8') == 'US'
    assert web.geo_country('10.0.0.1') is None
    monkeypatch.setenv('AB_GEOIP_PATH', 'missing-explicit-file.mmdb')
    assert web.geo_country('8.8.8.8') is None


def test_country_and_device_are_captured_before_ip_masking(tmp_path, monkeypatch):
    from ablab.settings import Deployment
    monkeypatch.delenv('AB_GEOIP_PATH', raising=False)
    target = TestClient(web.create_target(tmp_path, deployment=Deployment('https://admin.example.com','https://target.example.com')),
                        base_url='https://target.example.com', client=('127.0.0.1',1))
    target.get('/', headers={'x-real-ip':'8.8.8.8','x-forwarded-proto':'https','user-agent':'Mozilla/5.0 (Linux; Android 13; SM-S918B Build/TP1A) Chrome/120.0.0.0 Mobile Safari/537.36'})
    row = Store(tmp_path).logs()['items'][0]
    assert row['country'] == 'US'
    assert row['ip'] == '8.8.8.0/24'
    assert row['device_details']['model'] == 'SM-S918B'
