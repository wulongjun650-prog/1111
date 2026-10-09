"""aaPanel wire contracts; fake only external HTTP, retain real adapter checks."""
import hashlib
import json
import os
from urllib.parse import parse_qs

import httpx
import pytest

from ablab.provisioning import ProvisioningError


IDENTITY = {'site_id': 'a' * 32, 'domain': 'new.example.com',
            'path': '/www/wwwroot/ab-lab-sites/' + 'a' * 32, 'owner': 'b' * 32}


def adapter(handler, writes=False):
    from ablab.panel_sites import PanelSites
    return PanelSites('http://127.0.0.1:34734', 'example-key', httpx.MockTransport(handler), writes_enabled=writes)


def test_writable_local_panel_rejects_hard_linked_token_file(tmp_path):
    from ablab.panel_sites import PanelSites

    source = tmp_path / 'source.json'
    linked = tmp_path / 'api.json'
    source.write_text(json.dumps({
        'open': True, 'limit_addr': ['127.0.0.1'], 'token': 'a' * 32,
    }), encoding='utf-8')
    source.chmod(0o600)
    os.link(source, linked)
    with pytest.raises(ProvisioningError):
        PanelSites.from_local_config(
            'http://127.0.0.1:34734', linked, writes_enabled=True)


def fixture(sites=(), domains=(), create=None, bindings=()):
    requests = []

    def respond(request):
        fields = {key: values[0] for key, values in parse_qs(request.content.decode(), keep_blank_values=True).items()}
        action = request.url.params['action']
        assert set(request.url.params) == {'action'}
        expected = hashlib.md5((fields['request_time'] + hashlib.md5(b'example-key').hexdigest()).encode()).hexdigest()
        assert fields['request_token'] == expected
        requests.append((action, fields))
        if action == 'getData':
            assert request.url.path == '/v2/data'
            if fields['table'] == 'domain':
                assert fields['list'] == 'True' and fields['search'] == ''
                message = list(domains)
            elif fields['table'] == 'binding':
                assert fields['list'] == 'True' and fields['search'] == ''
                message = list(bindings)
            else:
                assert fields['table'] == 'sites' and fields['type'] == '-1'
                assert fields['search'] == IDENTITY['domain']
                message = {'data': list(sites) if fields['p'] == '1' else [], 'page': ''}
            return httpx.Response(200, json={'status': 0, 'message': message})
        assert action == 'AddSite' and request.url.path == '/v2/site'
        return httpx.Response(200, json=create or {'status': 0, 'message': {
            'siteStatus': True, 'siteId': 22, 'ftpStatus': False, 'databaseStatus': False, 'ssl': False}})

    return respond, requests


def owned_site(**changes):
    result = {'id': 22, 'name': IDENTITY['domain'], 'path': IDENTITY['path'],
              'ps': 'ab-lab:' + IDENTITY['site_id'] + ':' + IDENTITY['owner'], 'status': '1'}
    result.update(changes)
    return result


def domain(name, pid=22):
    return {'id': 1, 'pid': pid, 'name': name, 'port': 80, 'addtime': '2026-09-20'}


def test_existing_owned_site_is_recovered_using_exact_alias_and_marker():
    response, requests = fixture([owned_site()], [domain(IDENTITY['domain'])])
    assert adapter(response).inspect(IDENTITY['domain'], IDENTITY['path']) == dict(IDENTITY, panel_id=22)
    assert all(action == 'getData' for action, _ in requests)


@pytest.mark.parametrize('rows,aliases', [
    ([owned_site(ps='existing customer site')], [domain(IDENTITY['domain'])]),
    ([], [domain(IDENTITY['domain'], 99)]),
    ([], [domain('*.example.com', 99)]),
    ([owned_site()], [domain(IDENTITY['domain']), domain('extra.example.com')]),
    ([owned_site(path='/www/wwwroot/other')], [domain(IDENTITY['domain'])]),
    ([owned_site()], []),
])
def test_foreign_alias_wildcard_path_and_added_aliases_block_adoption(rows, aliases):
    response, _ = fixture(rows, aliases)
    with pytest.raises(ProvisioningError):
        adapter(response).inspect(IDENTITY['domain'], IDENTITY['path'])


def test_unrelated_wildcard_and_search_substring_do_not_claim_target():
    response, _ = fixture([owned_site(id=99, name='not-new.example.com')], [domain('*.other.example.com', 99)])
    assert adapter(response).inspect(IDENTITY['domain'], IDENTITY['path']) is None


def test_static_create_uses_exact_path_no_database_ftp_or_automatic_certificate():
    response, requests = fixture()
    assert adapter(response, writes=True).create_static(IDENTITY) == 22
    fields = requests[-1][1]
    assert requests[-1][0] == 'AddSite'
    assert json.loads(fields['webname']) == {'domain': IDENTITY['domain'], 'domainlist': [], 'count': 0}
    assert fields['path'] == IDENTITY['path']
    assert fields['version'] == '00'
    assert fields['ftp'] == fields['sql'] == fields['is_create_default_file'] == 'false'
    assert fields['set_ssl'] == fields['force_ssl'] == fields['ssl_auto'] == '0'
    assert fields['ps'] == owned_site()['ps']


def test_writes_are_disabled_by_default():
    response, requests = fixture()
    with pytest.raises(ProvisioningError):
        adapter(response).create_static(IDENTITY)
    assert requests == []


def test_create_rechecks_conflict_immediately_before_sending():
    response, requests = fixture([], [domain(IDENTITY['domain'], 99)])
    with pytest.raises(ProvisioningError):
        adapter(response, writes=True).create_static(IDENTITY)
    assert all(action == 'getData' for action, _ in requests)


@pytest.mark.parametrize('patch', [{'site_id': '../x'}, {'owner': '\ninclude x;'},
                                 {'domain': 'new.example.com;'}, {'path': '/www/wwwroot/existing'}])
def test_invalid_identity_is_rejected_before_http(patch):
    response, requests = fixture()
    with pytest.raises(ValueError):
        adapter(response, writes=True).create_static(dict(IDENTITY, **patch))
    assert requests == []


@pytest.mark.parametrize('result', [
    {'status': False, 'message': 'example-key'},
    {'status': 0, 'message': {'siteStatus': False, 'siteId': 22}},
    {'status': 0, 'message': {'siteStatus': True, 'siteId': True}},
    {'status': 0, 'message': {'siteStatus': True, 'siteId': 0}},
])
def test_incompatible_create_results_fail_without_leaking_response(result):
    response, _ = fixture(create=result)
    with pytest.raises(ProvisioningError) as error:
        adapter(response, writes=True).create_static(IDENTITY)
    assert 'example-key' not in str(error.value)


def test_repeated_pagination_does_not_silently_truncate_conflict_check():
    def repeat(request):
        fields = parse_qs(request.content.decode())
        if fields['table'] in (['domain'], ['binding']):
            message = []
        else:
            message = {'data': [owned_site(name='not-new.example.com')], 'page': ''}
        return httpx.Response(200, json={'status': 0, 'message': message})
    with pytest.raises(ProvisioningError):
        adapter(repeat).inspect(IDENTITY['domain'], IDENTITY['path'])


def test_timeout_is_not_retried_or_exposed():
    response, requests = fixture()

    def timeout(request):
        if request.url.params['action'] == 'AddSite':
            requests.append(('AddSite', {}))
            raise httpx.ReadTimeout('example-key', request=request)
        return response(request)

    with pytest.raises(ProvisioningError) as error:
        adapter(timeout, writes=True).create_static(IDENTITY)
    assert [x[0] for x in requests].count('AddSite') == 1
    assert 'example-key' not in str(error.value)


@pytest.mark.parametrize('binding,target', [
    ('bücher.example.com', 'xn--bcher-kva.example.com'),
    ('*.bücher.example.com', 'shop.xn--bcher-kva.example.com'),
])
def test_unicode_foreign_alias_blocks_ascii_create(binding, target):
    creates = []

    def respond(request):
        fields = parse_qs(request.content.decode())
        if request.url.params['action'] == 'AddSite':
            creates.append(request)
            message = {'siteStatus': True, 'siteId': 22}
        elif fields['table'] == ['binding']:
            message = []
        else:
            message = [domain(binding, 99)] if fields['table'] == ['domain'] else {'data': []}
        return httpx.Response(200, json={'status': 0, 'message': message})

    with pytest.raises(ProvisioningError):
        adapter(respond, writes=True).create_static(dict(IDENTITY, domain=target))
    assert creates == []


@pytest.mark.parametrize('name,pid', [('new.example.com', 99), ('*.example.com', 99),
                                     ('unrelated.example.com', True)])
def test_subdirectory_binding_conflict_or_invalid_owner_blocks_create(name, pid):
    binding = {'id': 8, 'pid': pid, 'domain': name, 'path': 'shop', 'port': 80, 'addtime': '2026-09-21'}
    response, requests = fixture(bindings=[binding])
    with pytest.raises(ProvisioningError):
        adapter(response, writes=True).create_static(IDENTITY)
    assert all(action != 'AddSite' for action, _ in requests)


def test_owned_site_with_added_subdirectory_cannot_be_reconfigured():
    response, _ = fixture([owned_site()], [domain(IDENTITY['domain'])], bindings=[
        {'id': 8, 'pid': 22, 'domain': 'shop.example.net', 'path': 'shop', 'port': 80, 'addtime': '2026-09-21'}])
    with pytest.raises(ProvisioningError):
        adapter(response).inspect(IDENTITY['domain'], IDENTITY['path'])


def _panel(respond):
    return adapter(respond, writes=True)


def _delete_handler(sites, domains):
    requests = []

    def respond(request):
        fields = {key: values[0] for key, values in parse_qs(request.content.decode(), keep_blank_values=True).items()}
        action = request.url.params['action']
        expected = hashlib.md5((fields['request_time'] + hashlib.md5(b'example-key').hexdigest()).encode()).hexdigest()
        assert fields['request_token'] == expected
        requests.append((action, fields))
        if action == 'getData':
            if fields['table'] == 'domain':
                message = list(domains)
            elif fields['table'] == 'binding':
                message = []
            else:
                message = {'data': list(sites) if fields['p'] == '1' else []}
            return httpx.Response(200, json={'status': 0, 'message': message})
        assert action == 'DeleteSite' and request.url.path == '/v2/site'
        assert fields['id'] == '22'
        assert fields['webname'] == IDENTITY['domain']
        assert fields['ftp'] == '0' and fields['database'] == '0' and fields['path'] == '1'
        return httpx.Response(200, json={'status': 0, 'message': True})

    return respond, requests


def test_delete_owned_removes_only_the_inspected_site():
    respond, requests = _delete_handler([owned_site()], [domain(IDENTITY['domain'])])
    assert _panel(respond).delete_owned(IDENTITY['domain'], IDENTITY['path'], 22) == 22
    assert [action for action, _fields in requests].count('DeleteSite') == 1


def test_delete_owned_leaves_a_missing_or_foreign_site():
    empty, requests = _delete_handler([], [])
    assert _panel(empty).delete_owned(IDENTITY['domain'], IDENTITY['path'], 22) is None
    assert all(action != 'DeleteSite' for action, _fields in requests)
    present, requests = _delete_handler([owned_site()], [domain(IDENTITY['domain'])])
    with pytest.raises(ProvisioningError, match='拒绝删除'):
        _panel(present).delete_owned(IDENTITY['domain'], IDENTITY['path'], 99)
    assert all(action != 'DeleteSite' for action, _fields in requests)
    foreign, requests = _delete_handler([owned_site(ps='not-ours')], [domain(IDENTITY['domain'])])
    with pytest.raises(ProvisioningError):
        _panel(foreign).delete_owned(IDENTITY['domain'], IDENTITY['path'], 22)
    assert all(action != 'DeleteSite' for action, _fields in requests)


def test_unrelated_subdirectory_does_not_block_new_site():
    response, requests = fixture(bindings=[
        {'id': 8, 'pid': 99, 'domain': 'shop.example.net', 'path': 'shop', 'port': 80, 'addtime': '2026-09-21'}])
    assert adapter(response, writes=True).create_static(IDENTITY) == 22
    assert any(fields.get('table') == 'binding' for _, fields in requests)
