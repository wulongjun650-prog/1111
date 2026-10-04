import io
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ablab.web import create_admin, create_target
from test_agent_access import accounts, add


@pytest.fixture
def editor(tmp_path):
    app = create_admin(tmp_path)
    admin = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    target = TestClient(create_target(tmp_path), base_url='http://127.0.0.1:8766', client=('127.0.0.1', 50001))
    return admin, target, app.state.store


def upload_bundle(client, slot='A', prefix='/api', publish=True):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('index.html', '<a href="https://old.test">Original</a>')
        archive.writestr('assets/app.js', 'location.href = "https://old.test";')
        archive.writestr('assets/style.css', 'body { color: red; }')
        archive.writestr('other.htm', '\ufeff<p>Unicode 中文</p>')
        archive.writestr('module.mjs', 'export const url = "https://old.test";')
        archive.writestr('assets/picture.png', b'\x89PNG\r\n\x1a\n\x00asset')
        archive.writestr('data.json', '{"keep":true}')
    result = client.post(f'{prefix}/upload/{slot}?name=bundle.zip', content=data.getvalue())
    assert result.status_code == 200, result.text
    version = result.json()['version']['id']
    if publish:
        assert client.post(f'{prefix}/publish/{slot}', json={'version_id': version}).status_code == 200
    return version


def save(client, version, path='index.html', content='<h1>Updated</h1>', slot='A', expected=None, prefix='/api'):
    return client.post(f'{prefix}/source/{slot}/{version}', json={
        'path': path, 'content': content, 'expected_published': expected,
    })


@pytest.mark.parametrize('slot', ['A', 'B'])
def test_edit_saves_new_published_bundle_and_preserves_history(editor, slot):
    admin, target, store = editor
    original = upload_bundle(admin, slot)
    state = admin.get('/api/state').json()
    state['config']['routing'] = 'FORCE_' + slot
    assert admin.put('/api/config', json={'config': state['config'], 'revision': state['revision']}).status_code == 200
    root = store.pages / original
    original_files = {str(file.relative_to(root)): file.read_bytes() for file in root.rglob('*') if file.is_file()}
    listing = admin.get(f'/api/source/{slot}/{original}')
    assert listing.status_code == 200, listing.text
    assert listing.json()['published_version'] == original
    assert {item['path'] for item in listing.json()['files']} == {'index.html', 'assets/app.js', 'assets/style.css', 'other.htm', 'module.mjs'}
    assert all(item['bytes'] > 0 for item in listing.json()['files'])
    for path, content in [('index.html', '<a href="https://new.test">Updated 中文</a>'),
                          ('assets/app.js', 'location.href = "https://new.test";'),
                          ('assets/style.css', 'body { color: blue; }'),
                          ('other.htm', '<p>New 中文</p>'),
                          ('module.mjs', 'export const url = "https://new.test";')]:
        current = store.slots()[slot]
        read = admin.get(f'/api/source/{slot}/{current}', params={'path': path})
        assert read.status_code == 200, read.text
        assert read.json()['path'] == path and isinstance(read.json()['content'], str)
        result = save(admin, current, path, content, slot, current)
        assert result.status_code == 200, result.text
        version = result.json()['version']
        assert version['id'] != current and version['slot'] == slot
        assert version['files'] == 7 and result.json()['published_version'] == version['id']
        assert store.slots()[slot] == version['id']
        served = target.get('/' + path)
        assert served.status_code == 200 and served.content.decode('utf-8-sig') == content
    assert {str(file.relative_to(root)): file.read_bytes() for file in root.rglob('*') if file.is_file()} == original_files
    assert target.get('/assets/picture.png').content == b'\x89PNG\r\n\x1a\n\x00asset'
    assert target.get('/data.json').json() == {'keep': True}
    assert len(store.versions()) == 6


def test_unpublished_draft_can_be_edited_and_published(editor):
    admin, _, store = editor
    original = upload_bundle(admin, publish=False)
    assert admin.get(f'/api/source/A/{original}').json()['published_version'] is None
    response = save(admin, original)
    assert response.status_code == 200, response.text
    assert store.slots()['A'] == response.json()['version']['id']


@pytest.mark.parametrize('path', ['../index.html', '/index.html', 'assets\\app.js', 'assets/%2e%2e/index.html',
                                  'assets/app.js:stream', 'assets/picture.png', 'data.json'])
def test_invalid_or_noneditable_path_cannot_read_or_publish(editor, path):
    admin, _, store = editor
    original = upload_bundle(admin)
    assert admin.get(f'/api/source/A/{original}', params={'path': path}).status_code == 400
    assert save(admin, original, path=path, expected=original).status_code == 400
    assert store.slots()['A'] == original
    assert len(store.versions()) == 1 and {p.name for p in store.pages.iterdir()} == {original}


def test_wrong_slot_unknown_version_or_missing_file_cannot_read_or_publish(editor):
    admin, _, store = editor
    original = upload_bundle(admin)
    for slot, version, path in [('B', original, 'index.html'), ('A', '0' * 32, 'index.html'), ('A', original, 'missing.js')]:
        assert admin.get(f'/api/source/{slot}/{version}', params={'path': path}).status_code == 404
        assert save(admin, version, path=path, slot=slot, expected=original).status_code == 404
    assert admin.get('/api/source/A/not-a-version').status_code == 422
    assert admin.get(f'/api/source/C/{original}').status_code == 422
    assert len(store.versions()) == 1


@pytest.mark.parametrize('content', ['<?php echo 1; ?>', '<%= secret %>', 'text\x00binary', '#!/bin/sh\necho bad'])
def test_static_content_validation_rejects_edits_without_orphans(editor, content):
    admin, _, store = editor
    original = upload_bundle(admin)
    assert save(admin, original, content=content, expected=original).status_code == 400
    assert store.slots()['A'] == original
    assert len(store.versions()) == 1 and {p.name for p in store.pages.iterdir()} == {original}


def test_source_payload_allows_large_text_but_enforces_file_limit(editor):
    admin, _, store = editor
    original = upload_bundle(admin)
    result = save(admin, original, content='x' * (300 * 1024), expected=original)
    assert result.status_code == 200, result.text
    current = result.json()['version']['id']
    oversized = save(admin, current, content='x' * (20 * 1024 * 1024 + 1), expected=current)
    assert oversized.status_code == 400, oversized.text
    assert store.slots()['A'] == current
    assert len(store.versions()) == 2 and {p.name for p in store.pages.iterdir()} == {original, current}


def test_concurrent_save_keeps_only_one_winner_and_no_orphan_version(editor):
    admin, _, store = editor
    original = upload_bundle(admin)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda text: save(admin, original, content=text, expected=original), ['first', 'second']))
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(result.json()['version']['id'] for result in results if result.status_code == 200)
    assert store.slots()['A'] == winner
    assert len(store.versions()) == 2 and {p.name for p in store.pages.iterdir()} == {original, winner}
    assert save(admin, original, expected=None).status_code == 409
    assert len(store.versions()) == 2


def test_source_saves_keep_csrf_origin_and_strict_body_validation(editor):
    admin, _, store = editor
    original = upload_bundle(admin)
    endpoint = f'/api/source/A/{original}'
    body = {'path': 'index.html', 'content': 'new', 'expected_published': original}
    assert admin.post(endpoint, json=body, headers={'X-CSRF-Token': 'bad'}).status_code == 403
    assert admin.post(endpoint, json=body, headers={'Origin': 'https://attacker.test'}).status_code == 403
    for changes in [{'expected_published': 'bad'}, {'content': 123}, {'extra': True}]:
        assert admin.post(endpoint, json=body | changes).status_code == 422
    assert len(store.versions()) == 1


def test_agents_can_edit_only_their_own_sites(accounts):
    app, _, _, one, two, _, _ = accounts
    first, second = add(one, 'one-edit.test'), add(two, 'two-edit.test')
    first_prefix, second_prefix = f"/api/sites/{first['id']}", f"/api/sites/{second['id']}"
    first_id = upload_bundle(one, prefix=first_prefix)
    second_id = upload_bundle(two, prefix=second_prefix)
    assert one.get(f'{first_prefix}/source/A/{first_id}').status_code == 200
    assert save(one, first_id, expected=first_id, prefix=first_prefix).status_code == 200
    for prefix, version in [(second_prefix, second_id), (first_prefix, second_id), ('/api', first_id)]:
        assert one.get(f'{prefix}/source/A/{version}').status_code == 404
        assert one.get(f'{prefix}/source/A/{version}', params={'path': 'index.html'}).status_code == 404
        assert save(one, version, expected=version, prefix=prefix).status_code == 404
    assert app.state.registry.store(second['id']).slots()['A'] == second_id


def test_edit_revalidates_preserved_assets_and_total_bundle_limit(editor, monkeypatch):
    admin, _, store = editor
    original = upload_bundle(admin)
    root = store.pages / original
    total = sum(file.stat().st_size for file in root.rglob('*') if file.is_file())
    monkeypatch.setattr('ablab.archives.MAX_TOTAL', total + 10)
    assert save(admin, original, content='x' * 1000, expected=original).status_code == 400
    (root / 'assets' / 'app.js').write_bytes(b'\xff\xfeinvalid UTF-8')
    assert admin.get(f'/api/source/A/{original}', params={'path': 'assets/app.js'}).status_code == 400
    assert save(admin, original, content='new', expected=original).status_code == 400
    assert store.slots()['A'] == original
    assert len(store.versions()) == 1 and {p.name for p in store.pages.iterdir()} == {original}


@pytest.mark.parametrize('replace_root', [False, True])
def test_source_rejects_directory_links_and_windows_junctions(editor, tmp_path, replace_root):
    admin, _, store = editor
    original = upload_bundle(admin)
    root = store.pages / original
    outside = tmp_path / 'private'
    outside.mkdir()
    (outside / 'index.html').write_text('PRIVATE SOURCE', encoding='utf-8')
    link = root if replace_root else root / 'linked'
    if replace_root:
        root.rename(store.pages / 'saved-original')
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        for path in [None, 'index.html', 'linked/index.html']:
            response = admin.get(f'/api/source/A/{original}', params={} if path is None else {'path': path})
            assert response.status_code == 400
            assert 'PRIVATE SOURCE' not in response.text
        assert save(admin, original, expected=original).status_code == 400
        assert store.slots()['A'] == original and len(store.versions()) == 1
        assert (outside / 'index.html').read_text(encoding='utf-8') == 'PRIVATE SOURCE'
    finally:
        if os.name == 'nt':
            link.rmdir()
        else:
            link.unlink()


def test_nested_bundle_uses_import_file_limit_without_counting_implicit_directories(editor, monkeypatch):
    admin, _, store = editor
    monkeypatch.setattr('ablab.archives.MAX_FILES', 2)
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('index.html', 'Original')
        archive.writestr('one/two/three/four/source.js', 'const original = true;')
    upload = admin.post('/api/upload/A?name=nested.zip', content=data.getvalue())
    assert upload.status_code == 200, upload.text
    original = upload.json()['version']['id']
    response = save(admin, original)
    assert response.status_code == 200, response.text
    assert (store.pages / response.json()['version']['id'] / 'one/two/three/four/source.js').read_text() == 'const original = true;'


def test_unreadable_asset_directory_cannot_be_silently_dropped_by_save(editor, monkeypatch):
    admin, _, store = editor
    original = upload_bundle(admin)
    inaccessible = store.pages / original / 'assets'
    scandir = os.scandir

    def denied_scandir(path):
        if os.fspath(path) == str(inaccessible):
            raise PermissionError('injected filesystem access failure')
        return scandir(path)

    monkeypatch.setattr(os, 'scandir', denied_scandir)
    assert save(admin, original, expected=original).status_code == 400
    assert store.slots()['A'] == original
    assert len(store.versions()) == 1 and {p.name for p in store.pages.iterdir()} == {original}
