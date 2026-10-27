import pytest
from fastapi.testclient import TestClient

from ablab.detect_mode import FetchError, _require_public_address, parse_domain
from ablab.web import create_admin

PAGE = '''<!doctype html>
<meta charset="utf-8"><title>名单甲</title>
<script>
function getContactFlowMode() {
  const params = new URLSearchParams(location.search);
  const code = String(params.get('code') || params.get('mode') || 'ABC').trim().toUpperCase();
  return code === 'CBA' ? 'CBA' : 'ABC';
}
const contactFlowMode = getContactFlowMode();
const useContactGate = contactFlowMode === 'ABC';
location.href = "https://api.whatsapp.com/send?phone=85290000001";
</script>
'''


def client(app):
    return TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 10000))


def test_private_addresses_are_not_opened():
    for raw in ('127.0.0.1', '10.1.1.1', '169.254.169.254', '::1'):
        with pytest.raises(FetchError):
            _require_public_address(raw)
    _require_public_address('8.8.8.8')


def test_a_typed_domain_need_not_belong_to_this_system(tmp_path):
    app = create_admin(tmp_path)
    admin = client(app)
    headers = {'origin': 'http://127.0.0.1:8765', 'x-csrf-token': app.state.csrf}
    assert 'data-tab="detect"' in admin.get('/').text
    pages = {'watch.example': PAGE}
    app.state.detect_fetch = lambda domain: pages[domain]
    added = admin.post('/api/detect-mode', json={'domain': 'https://Watch.Example/path'}, headers=headers)
    assert added.status_code == 200, added.text
    body = added.json()
    assert body['targets'][0]['domain'] == 'watch.example'
    assert body['targets'][0]['questionnaire_label'] == '先出问卷'
    assert '85290000001' in body['targets'][0]['phones']
    assert body['events'][0]['kind'] == 'baseline'
    assert 'watch.example' not in {site['domain'] for site in admin.get('/api/sites').json()['sites']}

    pages['watch.example'] = PAGE.replace("|| 'ABC'", "|| 'CBA'", 1).replace('85290000001', '85290000002')
    changed = admin.post('/api/detect-mode/check', headers=headers)
    assert changed.status_code == 200, changed.text
    kinds = {item['kind'] for item in changed.json()['events']}
    assert {'questionnaire', 'phone'} <= kinds
    assert 'other' not in kinds
    assert changed.json()['targets'][0]['questionnaire_label'] == '已跳过问卷'

    pages['watch.example'] = pages['watch.example'].replace('名单甲', '名单乙', 1)
    edited = admin.post('/api/detect-mode/check', headers=headers)
    assert edited.status_code == 200, edited.text
    fresh = [item for item in edited.json()['events'] if item['detail'] == '首页除问卷和号码外有改动']
    assert len(fresh) == 1

    removed = admin.delete('/api/detect-mode', params={'domain': 'watch.example'}, headers=headers)
    assert removed.status_code == 200, removed.text
    assert removed.json()['targets'] == []


def test_domain_text_accepts_a_bare_host():
    assert parse_domain('  Example.com ') == 'example.com'
    with pytest.raises(ValueError):
        parse_domain('https://user:pass@example.com')
