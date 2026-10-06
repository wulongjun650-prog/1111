"""Real certificate/file transactions; only external Nginx/panel calls are faked."""
import json
import subprocess
from datetime import timedelta

import pytest

from ablab.provisioning import ProvisioningError
from test_certificate_versions import publication, publish, snapshot
from test_certificates import material, NOW, PEM
from test_nginx_entry import setup, IDENTITY, BASELINE


@pytest.fixture
def deployment(tmp_path, publication):
    from ablab.nginx_tls import TlsEntry
    nginx, config, remote, calls = setup(tmp_path)
    nginx.capture_created(IDENTITY, 22)
    nginx.configure(IDENTITY, 22)
    calls.clear()
    version = publish(publication)
    return TlsEntry(nginx, publication[0]), config, remote, calls, version


def configure(deployment, **overrides):
    tls, _, _, _, version = deployment
    args = dict(identity=IDENTITY, panel_id=22, digest=version.name, authorize=lambda: True, now=NOW)
    args.update(overrides)
    return tls.configure(**args)


def pending(tls):
    return tls.nginx.root / (IDENTITY['site_id'] + '.tls.pending')


def test_switch_and_repeat_do_not_rewrite_or_reload(deployment):
    tls, config, _, calls, version = deployment
    previous = config.read_bytes()
    assert configure(deployment) is True
    output = config.read_text()
    assert 'listen 443 ssl;' in output
    assert 'ssl_protocols TLSv1.2 TLSv1.3;' in output
    assert f'ssl_certificate "{version.as_posix()}/fullchain.pem";' in output
    assert 'return 308 https://new.example.com$request_uri;' in output
    assert 'location ^~ /.well-known/acme-challenge/' in output
    assert 'proxy_pass http://127.0.0.1:8766;' in output
    assert 'proxy_set_header Host new.example.com;' in output
    assert 'proxy_set_header X-Forwarded-For "";' in output
    assert 'proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;' in output
    assert 'proxy_set_header X-Real-IP $http_cf_connecting_ip' not in output
    assert [cmd[1:] for cmd in calls] == [['-t'], ['-t'], ['-s', 'reload']]
    assert not pending(tls).exists()
    assert any(p.read_bytes() == previous for p in config.parent.rglob('*') if p.is_file())
    before, count = snapshot(config.parent), len(calls)
    assert configure(deployment) is False
    assert snapshot(config.parent) == before and len(calls) == count


def test_renewal_preserves_previous_certificate_and_configuration(deployment, publication, material):
    tls, config, _, calls, old = deployment
    configure(deployment)
    previous, old_files = config.read_bytes(), snapshot(old)
    chain = material['leaf'](end=NOW + timedelta(days=100)).public_bytes(PEM)
    chain += material['intermediate'].public_bytes(PEM)
    new = publish(publication, fullchain=chain)
    assert configure(deployment, digest=new.name)
    assert new.as_posix() in config.read_text() and old.as_posix() not in config.read_text()
    assert snapshot(old) == old_files
    assert any(p.read_bytes() == previous for p in config.parent.rglob('*') if p.is_file())
    assert sum('reload' in cmd for cmd in calls) == 2
    assert not configure(deployment, digest=new.name)


@pytest.mark.parametrize('defect', ['no-receipt', 'not-http', 'manual', 'owner', 'disabled', 'pause', 'truthy', 'expired', 'old-pending'])
def test_rejects_unsafe_initial_state_without_changes(deployment, defect):
    tls, config, remote, calls, _ = deployment
    if defect == 'no-receipt': (tls.nginx.root / (IDENTITY['site_id'] + '.json')).unlink()
    elif defect == 'not-http': config.write_bytes(BASELINE)
    elif defect == 'manual': config.write_text('# operator-owned edit')
    elif defect == 'owner': remote['owner'] = 'c' * 32
    elif defect == 'disabled': tls.nginx.writes_enabled = False
    elif defect == 'old-pending': (tls.nginx.root / (IDENTITY['site_id'] + '.pending')).write_text('{}')
    before = snapshot(config.parent.parent)
    kwargs = {'authorize': lambda: False} if defect == 'pause' else {}
    if defect == 'truthy': kwargs['authorize'] = lambda: 1
    if defect == 'expired': kwargs['now'] = NOW + timedelta(days=100)
    with pytest.raises(ProvisioningError): configure(deployment, **kwargs)
    assert snapshot(config.parent.parent) == before and calls == []


@pytest.mark.parametrize('stage', [1, 2, 3])
def test_nginx_failure_retains_pending_and_blocks_retry(deployment, stage):
    tls, config, _, calls, _ = deployment
    previous = config.read_bytes()

    def runner(args, **kwargs):
        calls.append(args)
        if len(calls) == stage:
            raise subprocess.TimeoutExpired(args, 15)
        return subprocess.CompletedProcess(args, 0, '', '')

    tls.nginx.runner = runner
    with pytest.raises(ProvisioningError): configure(deployment)
    assert pending(tls).exists() is (stage != 1)
    if stage < 3: assert config.read_bytes() == previous
    else: assert b'listen 443 ssl;' in config.read_bytes()
    if stage != 1:
        count = len(calls)
        with pytest.raises(ProvisioningError): configure(deployment)
        assert len(calls) == count
    assert not (tls.nginx.root / (IDENTITY['site_id'] + '.tls.json')).exists()


@pytest.mark.parametrize('defect', ['manual', 'owner', 'pause', 'certificate'])
@pytest.mark.parametrize('syntax_ok', [False, True])
def test_changes_during_validation_never_reload_or_clobber_operator(deployment, defect, syntax_ok):
    tls, config, remote, calls, version = deployment
    allowed = True

    def runner(args, **kwargs):
        nonlocal allowed
        calls.append(args)
        if len(calls) == 2:
            if defect == 'manual': config.write_text('# operator edit')
            elif defect == 'owner': remote['owner'] = 'c' * 32
            elif defect == 'pause': allowed = False
            else: (version / 'privkey.pem').write_text('private-marker')
            return subprocess.CompletedProcess(args, 0 if syntax_ok else 1, '', '')
        return subprocess.CompletedProcess(args, 0, '', '')

    tls.nginx.runner = runner
    with pytest.raises(ProvisioningError): configure(deployment, authorize=lambda: allowed)
    assert pending(tls).exists()
    assert not any('reload' in cmd for cmd in calls)
    if defect == 'manual': assert config.read_text() == '# operator edit'
    if defect in ('owner', 'pause'): assert 'listen 443 ssl;' in config.read_text()


def test_pause_after_reload_retains_pending_instead_of_claiming_complete(deployment):
    tls, _, _, calls, _ = deployment
    with pytest.raises(ProvisioningError):
        configure(deployment, authorize=lambda: not any('reload' in cmd for cmd in calls))
    assert pending(tls).exists()
    assert not (tls.nginx.root / (IDENTITY['site_id'] + '.tls.json')).exists()


def test_failed_renewal_restores_previous_tls_not_plain_http(deployment, publication, material):
    tls, config, _, calls, _ = deployment
    configure(deployment)
    before = config.read_bytes()
    calls.clear()
    chain = material['leaf'](end=NOW + timedelta(days=100)).public_bytes(PEM)
    chain += material['intermediate'].public_bytes(PEM)
    new = publish(publication, fullchain=chain)

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if len(calls) == 2 else 0, '', '')

    tls.nginx.runner = runner
    with pytest.raises(ProvisioningError): configure(deployment, digest=new.name)
    assert config.read_bytes() == before and b'listen 443 ssl;' in before
    assert pending(tls).exists() and not any('reload' in cmd for cmd in calls)


@pytest.mark.parametrize('after_write', [False, True])
def test_completion_write_failure_blocks_retry_after_reload(deployment, monkeypatch, after_write):
    from ablab import nginx_tls
    tls, config, _, calls, _ = deployment
    original = nginx_tls.exclusive_text

    def fail(path, text, *args, **kwargs):
        if path.name.endswith('.tls.json'):
            if after_write: original(path, text, *args, **kwargs)
            raise OSError('private-marker')
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(nginx_tls, 'exclusive_text', fail)
    with pytest.raises(ProvisioningError) as error: configure(deployment)
    assert 'private-marker' not in str(error.value)
    assert pending(tls).exists() and 'listen 443 ssl;' in config.read_text()
    count = len(calls)
    with pytest.raises(ProvisioningError): configure(deployment)
    assert len(calls) == count and calls[-1][-1] == 'reload'


@pytest.mark.parametrize('after_replace', [False, True])
def test_renewal_receipt_replacement_failure_blocks_retry(deployment, publication, material, monkeypatch, after_replace):
    from ablab import nginx_entry
    tls, config, _, calls, _ = deployment
    configure(deployment)
    previous = (tls.nginx.root / (IDENTITY['site_id'] + '.tls.json')).read_bytes()
    chain = material['leaf'](end=NOW + timedelta(days=100)).public_bytes(PEM)
    chain += material['intermediate'].public_bytes(PEM)
    new = publish(publication, fullchain=chain)
    original = nginx_entry.replace_preserving

    def fail(staged, target):
        if target.name.endswith('.tls.json'):
            if after_replace: original(staged, target)
            raise OSError('private-marker')
        return original(staged, target)

    monkeypatch.setattr(nginx_entry, 'replace_preserving', fail)
    with pytest.raises(ProvisioningError): configure(deployment, digest=new.name)
    assert pending(tls).exists() and new.as_posix() in config.read_text()
    assert any(p.read_bytes() == previous for p in tls.nginx.root.rglob('*') if p.is_file())
    count = len(calls)
    with pytest.raises(ProvisioningError): configure(deployment, digest=new.name)
    assert len(calls) == count and calls[-1][-1] == 'reload'


@pytest.mark.parametrize('defect', ['owner', 'version', 'desired', 'json'])
def test_invalid_committed_receipt_cannot_authorize_a_new_switch(deployment, defect):
    tls, config, _, calls, _ = deployment
    configure(deployment)
    state = tls.nginx.root / (IDENTITY['site_id'] + '.tls.json')
    saved = json.loads(state.read_text())
    if defect == 'owner': saved['identity']['owner'] = 'c' * 32
    elif defect == 'version': saved['version'] = '../foreign'
    elif defect == 'desired': saved['desired'] = '# operator'
    state.write_text('[]' if defect == 'json' else json.dumps(saved))
    before, count = snapshot(config.parent.parent), len(calls)
    with pytest.raises(ProvisioningError): configure(deployment)
    assert snapshot(config.parent.parent) == before and len(calls) == count


@pytest.mark.parametrize('path', ['/tmp/$secret', '/tmp/x;bad', '/tmp/x\nssl;', '/tmp/../x'])
def test_renderer_rejects_path_injection(path):
    from pathlib import Path
    from ablab.nginx_tls import render_tls_entry
    with pytest.raises((ValueError, ProvisioningError)):
        render_tls_entry(IDENTITY, Path(path))
