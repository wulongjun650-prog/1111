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
