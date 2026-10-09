import pytest
from fastapi.testclient import TestClient

from ablab.sites import Registry, normalize_domain
from ablab.store import Conflict, Store
from ablab.web import create_admin, create_target


@pytest.mark.parametrize('value', ['https://example.com', '*.example.com', 'example.com:443', 'localhost', '127.0.0.1', '../x', 'a..com', '-a.com', 'a.com/path', 'a.com\n'])
def test_invalid_domain(value):
    with pytest.raises(ValueError):
        normalize_domain(value)


def test_registry_isolation_migration(tmp_path):
    old = Store(tmp_path)
    config, revision = old.config()
    old.save_config(config.model_copy(update={'allowed_slot': 'A'}), revision)
    registry = Registry(tmp_path, default_domain='old.example.com', reserved=['admin.example.com'])
    assert Registry(tmp_path, default_domain='old.example.com').list()[0]['id'] == 'default'
    assert registry.store('default').config()[0].allowed_slot == 'A'
    assert (tmp_path / 'backups' / 'pre-multidomain.db').is_file()
    first = registry.add('ONE.Example.com')
    second = registry.add('two.example.com')
    assert first['domain'] == 'one.example.com'
    assert first['stage'] == 'unconfigured'
    with pytest.raises(Conflict):
        registry.add('one.example.com')
    with pytest.raises(ValueError):
        registry.add('admin.example.com')
    assert registry.store(first['id']).directory != registry.store(second['id']).directory
    assert registry.store(first['id']).count('1.1.1.1', 24, True) == 1
    assert registry.store(second['id']).count('1.1.1.1', 24, False) == 0
    with pytest.raises(KeyError):
        registry.store('../x')


def client(app, port):
    return TestClient(app, base_url=f'http://127.0.0.1:{port}', client=('127.0.0.1', 10000))


def test_scoped_api_content_and_preview(tmp_path):
    app = create_admin(tmp_path)
    admin = client(app, 8765)
    headers = {'origin': 'http://127.0.0.1:8765', 'x-csrf-token': app.state.csrf}
    def add(domain):
        response = admin.post('/api/sites', json={'domain': domain}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()['site']['id']
    a, b = add('one.example.com'), add('two.example.com')
    first = f'/api/sites/{a}'
    second = f'/api/sites/{b}'
    initial = admin.get(first + '/state').json()
    initial['config']['allowed_slot'] = 'A'
    assert admin.put(first + '/config', json={k: initial[k] for k in ('config', 'revision')}, headers=headers).status_code == 200
    assert admin.get(second + '/state').json()['config']['allowed_slot'] == 'B'
    version = admin.post(first + '/upload/A?name=index.html', content=b'<h1>site-one</h1>', headers=headers).json()['version']['id']
    assert admin.post(first + '/publish/A', json={'version_id': version}, headers=headers).status_code == 200
    assert admin.post(second + '/publish/A', json={'version_id': version}, headers=headers).status_code != 200
    url = admin.post(first + '/preview', json={'version_id': version}, headers=headers).json()['url']
    target = client(create_target(tmp_path), 8766)
    assert target.get(url).text == '<h1>site-one</h1>'
    assert target.get(url.replace(a, b)).status_code == 404
    assert 'site-one' in target.get(f'/_sites/{a}/').text
    assert target.get(admin.get(first + '/state').json()['target_url']).text == '<h1>site-one</h1>'
    assert target.get(f'/_sites/{b}/').status_code == 503
    assert target.get(admin.get(second + '/state').json()['target_url']).status_code == 503
    assert target.get('/', headers={'host': 'unknown.example.com'}).status_code == 403
    removed = admin.delete(f'/api/sites/{a}', headers=headers)
    assert removed.status_code == 200, removed.text
    assert removed.json()['domain'] == 'one.example.com'
    assert removed.json()['cloudflare_warning'] == ''
    assert a not in {site['id'] for site in admin.get('/api/sites').json()['sites']}
    assert not (tmp_path / 'sites' / a).exists()
    assert (tmp_path / 'erasures' / f'{a}.json').is_file()
    assert target.get(f'/_sites/{a}/').status_code == 404
    assert admin.delete('/api/sites/default', headers=headers).status_code == 400
    assert admin.get('/api/sites/unknown/state').status_code == 404
    assert admin.get('/api/state').json()['site']['id'] == 'default'
    assert admin.post('/api/sites', json={'domain': 'no-csrf.example.com'}).status_code == 403


def test_public_host_routing_and_no_cross_site_prefix(tmp_path):
    from ablab.settings import Deployment
    deployment = Deployment('https://admin.example.com', 'https://old.example.com')
    registry = Registry(tmp_path, default_domain='old.example.com')
    site = registry.add('new.example.com')
    registry.store(site['id']).add_links('B', ['https://example.com/new'])
    store = registry.store(site['id'])
    config, revision = store.config()
    store.save_config(config.model_copy(update={'content_mode': 'LINK'}), revision)
    target = TestClient(create_target(tmp_path, deployment=deployment), base_url='https://new.example.com', client=('127.0.0.1', 1000))
    target.headers.update({'x-real-ip': '1.1.1.1', 'x-forwarded-proto': 'https'})
    response = target.get('/', follow_redirects=False)
    assert response.headers['location'] == 'https://example.com/new'
    assert target.get('/_sites/default/').status_code == 404
    assert target.get('/', headers={'host': 'alien.example.com'}).status_code == 403
    registry.control(site['id'], 'pause')
    assert target.get('/').status_code == 403


def test_large_scoped_upload_and_directory_redirect(tmp_path):
    import io
    import zipfile
    app = create_admin(tmp_path)
    admin = client(app, 8765)
    admin.headers.update({'origin': 'http://127.0.0.1:8765', 'x-csrf-token': app.state.csrf})
    site_id = admin.post('/api/sites', json={'domain': 'nested.example.com'}).json()['site']['id']
    prefix = f'/api/sites/{site_id}'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('index.html', 'X' * 300000)
        zipped.writestr('nested/index.html', '<h1>nested</h1>')
    uploaded = admin.post(prefix + '/upload/B?name=site.zip', content=archive.getvalue())
    assert uploaded.status_code == 200
    vid = uploaded.json()['version']['id']
    admin.post(prefix + '/publish/B', json={'version_id': vid})
    target = client(create_target(tmp_path), 8766)
    path = f'/_sites/{site_id}/nested'
    assert target.get(path, follow_redirects=False).headers['location'] == path + '/'
    assert target.get(path).text == '<h1>nested</h1>'


def test_pause_generation_cannot_be_overwritten(tmp_path):
    registry = Registry(tmp_path)
    site = registry.add('pause.example.com')
    registry.control(site['id'], 'pause')
    assert not registry.transition(site['id'], site['generation'], 'active')
    assert registry.get(site['id'])['stage'] == 'paused'


def test_remove_drops_the_row_events_reputation_and_files(tmp_path):
    from ablab.reputation import GoogleReputation
    registry = Registry(tmp_path, default_domain='old.example.com')
    GoogleReputation(registry)
    site = registry.add('gone.example.com')
    registry.store(site['id']).add_links('B', ['https://example.com/gone'])
    with registry.connect() as db:
        db.execute('INSERT INTO google_reputation(site_id,result) VALUES(?,?)', (site['id'], '{}'))
        db.execute('INSERT INTO google_reputation_locks(site_id,token,until) VALUES(?,?,?)', (site['id'], 't', 1))
    directory = tmp_path / 'sites' / site['id']
    assert directory.is_dir()
    with pytest.raises(ValueError, match='原有站点不能删除'):
        registry.remove('default')
    snapshot = registry.remove(site['id'])
    assert snapshot['domain'] == 'gone.example.com'
    assert not directory.exists()
    with pytest.raises(KeyError):
        registry.get(site['id'])
    with registry.connect() as db:
        assert db.execute('SELECT 1 FROM site_events WHERE site_id=?', (site['id'],)).fetchone() is None
        assert db.execute('SELECT 1 FROM google_reputation WHERE site_id=?', (site['id'],)).fetchone() is None
        assert db.execute('SELECT 1 FROM google_reputation_locks WHERE site_id=?', (site['id'],)).fetchone() is None
        assert db.execute("SELECT 1 FROM sites WHERE id='default'").fetchone() is not None
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('keep')
    linked = registry.add('link.example.com')
    link = tmp_path / 'sites' / linked['id']
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        registry.remove(linked['id'])
    assert (outside / 'secret').read_text() == 'keep'
    assert registry.get(linked['id'])['domain'] == 'link.example.com'
