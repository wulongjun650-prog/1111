import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from ablab.contact_config import fill_contact_bytes, fill_contact_text
from ablab.web import create_admin
from test_b_redirects import _same_function


JUMP = '''
function buildWhatsAppUrls(message){
  return {phone:'85264150954', universal:'https://wa.me/85264150954', scheme:'whatsapp://send?phone=85264150954', intent:'intent://send#Intent;scheme=whatsapp;package=com.whatsapp;end'};
}
function goWhatsApp(){
  var urls = buildWhatsAppUrls('hi');
  location.href = urls.universal;
}
'''


def page(config, extra=''):
    return f'''<!doctype html>
<script>
const CONFIG = {{
{config}
}};
{JUMP}
</script>
<a href="https://wa.me/85264150954">go</a>
{extra}'''


@pytest.fixture
def console(tmp_path):
    app = create_admin(tmp_path)
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 5000))
    client.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    return client, app


def upload(client, html, scripts=None):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('index.html', html)
        for name, body in (scripts or {}).items():
            bundle.writestr(name, body)
    result = client.post('/api/upload/B?name=band.zip', content=archive.getvalue())
    assert result.status_code == 200, result.text
    version = result.json()['version']['id']
    assert client.post('/api/publish/B', json={'version_id': version}).status_code == 200
    return version


def add_link(client, app='band', url='https://band.us/n/example', note=''):
    result = client.post('/api/b-redirects/links', json={'app': app, 'urls': [url], 'note': note})
    assert result.status_code == 200, result.text
    return result.json()['links'][0]


def apply_link(client, scan, link):
    return client.post('/api/b-redirects/links/apply', json={
        'version_id': scan['version']['id'], 'link_id': link['id'], 'expected_published': scan['published_version']})


def published_text(app):
    version = app.state.store.slots()['B']
    return (app.state.store.pages / version / 'index.html').read_text(encoding='utf-8')


def test_replace_writes_missing_config_fields_and_leaves_the_jump(console):
    client, app = console
    original = page("  whatsappNumber: '85264150954',\n  eventEndpoint: ''")
    base = upload(client, original)
    before = (app.state.store.pages / base / 'index.html').read_bytes()
    scan = client.get('/api/b-redirects').json()
    assert scan['contact']['has_config'] is True
    assert scan['contact']['contact_app'] == {'present': False, 'value': ''}
    assert scan['contact']['band_url']['present'] is False
    assert scan['links'] == []
    link = add_link(client, note='群')
    assert client.get('/api/b-redirects').json()['links'][0]['url'] == link['url']
    result = apply_link(client, scan, link)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['changed'] == 1 and body['contact_app'] == 'added' and body['url'] == 'added'
    assert body['contact_value'] == 'band'
    assert body['version']['id'] != base
    text = published_text(app)
    assert 'contactApp: "band"' in text
    assert 'bandUrl: "https://band.us/n/example"' in text
    assert "whatsappNumber: '85264150954'" in text and "eventEndpoint: ''" in text
    assert 'https://wa.me/85264150954' in text
    _same_function(original, text, 'buildWhatsAppUrls')
    _same_function(original, text, 'goWhatsApp')
    assert (app.state.store.pages / base / 'index.html').read_bytes() == before
    again = client.get('/api/b-redirects').json()
    assert again['contact']['contact_app'] == {'present': True, 'value': 'band'}
    assert again['contact']['band_url'] == {'present': True, 'value': 'https://band.us/n/example'}
    assert again['contact']['kakao_url']['present'] is False
    kept = apply_link(client, again, link)
    assert kept.status_code == 200, kept.text
    assert kept.json()['changed'] == 0 and kept.json()['contact_app'] == 'present' and kept.json()['url'] == 'present'
    assert kept.json()['version']['id'] == again['published_version']
    assert published_text(app) == text


def test_existing_contact_app_and_link_stay_even_when_the_stored_url_differs(console):
    client, app = console
    original = page('  contactApp: "whatsapp",\n  bandLink: \'https://band.us/n/keep\',\n  whatsappNumber: \'85264150954\'')
    base = upload(client, original)
    before = (app.state.store.pages / base / 'index.html').read_bytes()
    scan = client.get('/api/b-redirects').json()
    assert scan['contact']['contact_app'] == {'present': True, 'value': 'whatsapp'}
    assert scan['contact']['band_url'] == {'present': True, 'value': 'https://band.us/n/keep'}
    link = add_link(client, url='https://band.us/n/other')
    result = apply_link(client, scan, link)
    assert result.status_code == 200, result.text
    assert result.json()['changed'] == 0
    assert result.json()['contact_value'] == 'whatsapp'
    assert app.state.store.slots()['B'] == base
    assert (app.state.store.pages / base / 'index.html').read_bytes() == before


def test_missing_url_is_added_without_overwriting_contact_app(console):
    client, app = console
    original = page('  contactApp: "whatsapp",\n  whatsappNumber: \'85264150954\'')
    upload(client, original)
    scan = client.get('/api/b-redirects').json()
    link = add_link(client, app='kakao', url='https://open.kakao.com/o/example')
    result = apply_link(client, scan, link)
    assert result.status_code == 200, result.text
    assert result.json()['changed'] == 1
    assert result.json()['contact_app'] == 'present' and result.json()['url'] == 'added'
    assert result.json()['contact_value'] == 'whatsapp'
    text = published_text(app)
    assert 'contactApp: "whatsapp"' in text
    assert 'kakaoUrl: "https://open.kakao.com/o/example"' in text
    assert 'kakaoLink' not in text and 'bandUrl' not in text
    _same_function(original, text, 'buildWhatsAppUrls')
    _same_function(original, text, 'goWhatsApp')


def test_page_without_config_is_left_unchanged(console):
    client, app = console
    html = '<!doctype html><a href="https://wa.me/85264150954">go</a>' + JUMP
    base = upload(client, html)
    before = (app.state.store.pages / base / 'index.html').read_bytes()
    scan = client.get('/api/b-redirects').json()
    assert scan['contact']['has_config'] is False
    link = add_link(client)
    result = apply_link(client, scan, link)
    assert result.status_code == 400
    assert '没有 CONFIG' in result.json()['detail']
    assert app.state.store.slots()['B'] == base
    assert (app.state.store.pages / base / 'index.html').read_bytes() == before


def test_comment_and_string_do_not_count_as_existing_fields():
    commented = page('  // contactApp: "band"\n  message: "set contactApp later",\n  whatsappNumber: \'852\'')
    text, status = fill_contact_text(commented, 'band', 'https://band.us/n/example', True)
    assert status['changed'] is True and status['contact_app'] == 'added' and status['url'] == 'added'
    assert 'contactApp: "band"' in text and 'bandUrl: "https://band.us/n/example"' in text
    assert '// contactApp: "band"' in text and 'set contactApp later' in text
    hidden = '''<p>const CONFIG = { contactApp: "band", bandUrl: "https://band.us/n/decoy" }</p>
<script>
const note = "const CONFIG = { contactApp: \\"x\\" }";
const CONFIG = { whatsappNumber: "1" };
</script>'''
    written, status = fill_contact_text(hidden, 'kakao', 'https://open.kakao.com/o/real', True)
    assert status['contact_app'] == 'added' and status['url'] == 'added'
    assert written.count('contactApp: "kakao"') == 1
    assert 'kakaoUrl: "https://open.kakao.com/o/real"' in written
    assert 'https://band.us/n/decoy' in written
    assert written.index('kakaoUrl:') > written.index('<script>')


def test_bom_and_unreadable_config_are_preserved():
    body = 'const CONFIG = {\n  whatsappNumber: "1"\n};\n'
    data, status = fill_contact_bytes(b'\xef\xbb\xbf' + body.encode(), 'band', 'https://band.us/n/example', False)
    assert data.startswith(b'\xef\xbb\xbf') and status['changed'] is True
    assert b'contactApp: "band"' in data
    broken = 'const CONFIG = { contactApp: "band'
    same, status = fill_contact_text(broken, 'band', 'https://band.us/n/example', False)
    assert same == broken and status['unreadable'] is True


def test_second_script_is_filled_when_index_already_has_the_fields(console):
    client, app = console
    index = page('  contactApp: "band",\n  bandUrl: "https://band.us/n/keep",\n  whatsappNumber: \'85264150954\'')
    script = 'const CONFIG = { whatsappNumber: "85264150954" };\n'
    upload(client, index, {'app.js': script})
    scan = client.get('/api/b-redirects').json()
    assert scan['contact']['contact_app']['present'] is False
    link = add_link(client, url='https://band.us/n/other')
    result = apply_link(client, scan, link)
    assert result.status_code == 200, result.text
    assert result.json()['changed'] == 1
    version = app.state.store.slots()['B']
    root = app.state.store.pages / version
    assert 'bandUrl: "https://band.us/n/keep"' in (root / 'index.html').read_text(encoding='utf-8')
    assert 'https://band.us/n/other' not in (root / 'index.html').read_text(encoding='utf-8')
    extra = (root / 'app.js').read_text(encoding='utf-8')
    assert 'contactApp: "band"' in extra and 'bandUrl: "https://band.us/n/other"' in extra


def test_pool_rejects_bad_urls_and_deletes_without_touching_the_page(console):
    client, app = console
    base = upload(client, page("  whatsappNumber: '85264150954'"))
    assert client.post('/api/b-redirects/links', json={'app': 'band', 'urls': ['http://band.us/n/x']}).status_code == 422
    assert client.post('/api/b-redirects/links', json={'app': 'kakao', 'urls': ['javascript:alert(1)']}).status_code == 422
    first = add_link(client, note='一')
    second = add_link(client, note='二')
    assert second['id'] == first['id'] and second['note'] == '二'
    kakao = add_link(client, app='kakao', url='https://band.us/n/example')
    assert kakao['id'] != first['id']
    assert client.delete(f"/api/b-redirects/links/{kakao['id']}").status_code == 200
    assert [item['app'] for item in client.get('/api/b-redirects').json()['links']] == ['band']
    assert app.state.store.slots()['B'] == base
    missing = client.post('/api/b-redirects/links/apply', json={'version_id': base, 'link_id': kakao['id'], 'expected_published': base})
    assert missing.status_code == 400
    scan = client.get('/api/b-redirects').json()
    stale = client.post('/api/b-redirects/links/apply', json={'version_id': scan['version']['id'], 'link_id': first['id'], 'expected_published': 'a' * 32})
    assert stale.status_code == 409
    assert app.state.store.slots()['B'] == base


def test_pool_stops_at_one_hundred(console):
    _client, app = console
    for index in range(100):
        app.state.store.add_contact_links('band', [f'https://band.us/n/{index}'], '')
    with pytest.raises(ValueError, match='100'):
        app.state.store.add_contact_links('kakao', ['https://open.kakao.com/o/overflow'], '')
