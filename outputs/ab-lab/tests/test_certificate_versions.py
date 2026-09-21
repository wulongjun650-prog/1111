"""Immutable publication: real certificates and files; no panel/CA calls."""
from datetime import timedelta
import json
import os

import pytest

from ablab.provisioning import ProvisioningError
from test_certificates import material, verifier, key_pem, PEM, NOW
from test_panel_sites import IDENTITY


@pytest.fixture
def publication(tmp_path, material):
    from ablab.certificate_versions import CertificateVersions
    base = tmp_path / 'certificates'
    base.mkdir(mode=0o700)
    chain = material['leaf']().public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    key = key_pem(material['key'])
    store = CertificateVersions(verifier(material), base, writes_enabled=True)
    return store, base, chain, key


def publish(publication, **overrides):
    store, _, chain, key = publication
    args = dict(identity=IDENTITY, panel_id=22, fullchain=chain, private_key=key,
                authorize=lambda: True, now=NOW)
    args.update(overrides)
    return store.publish(**args)


def snapshot(directory):
    return {str(p.relative_to(directory)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in directory.rglob('*') if p.is_file()}


def test_valid_version_is_complete_and_repeat_does_not_rewrite(publication):
    _, base, chain, key = publication
    version = publish(publication)
    assert version.parent == base / 'production' / 'deployed' / ('ab-' + 'a' * 32)
    assert len(version.name) == 64 and set(version.name) <= set('0123456789abcdef')
    assert (version / 'fullchain.pem').read_bytes() == chain
    assert (version / 'privkey.pem').read_bytes() == key
    ready = json.loads((version / 'ready.json').read_text())
    assert ready['identity'] == IDENTITY and ready['panel_id'] == 22
    assert ready['environment'] == 'production'
    assert 'PRIVATE KEY' not in repr(ready)
    before = snapshot(base)
    assert publish(publication) == version
    assert snapshot(base) == before


def test_renewed_certificate_gets_new_version_and_preserves_old(publication, material):
    old = publish(publication)
    before = snapshot(old)
    chain = material['leaf'](end=NOW + timedelta(days=100)).public_bytes(PEM)
    chain += material['intermediate'].public_bytes(PEM)
    new = publish(publication, fullchain=chain)
    assert new != old and new.parent == old.parent
    assert (new / 'fullchain.pem').read_bytes() == chain
    assert snapshot(old) == before


@pytest.mark.parametrize('permission', [False, True, 1, 'true', None])
def test_write_gate_requires_explicit_enablement_and_literal_authorization(publication, material, permission):
    from ablab.certificate_versions import CertificateVersions
    store, base, chain, key = publication
    # True authorization cannot enable the default-off store; other truthy values cannot authorize it.
    if permission is True:
        store = CertificateVersions(verifier(material), base)
    with pytest.raises(ProvisioningError):
        store.publish(IDENTITY, 22, chain, key, lambda: permission, now=NOW)
    assert list(base.iterdir()) == []


@pytest.mark.parametrize('panel_id', [True, 0, -1, '22', None])
def test_invalid_panel_identity_never_creates_files(publication, panel_id):
    with pytest.raises(ValueError):
        publish(publication, panel_id=panel_id)
    assert list(publication[1].iterdir()) == []


def test_staging_and_mismatched_key_are_rejected_before_filesystem_writes(publication, material):
    stage = material['leaf'](issuer=material['stage'], issuer_key=material['stage_key']).public_bytes(PEM)
    for overrides in ({'fullchain': stage}, {'private_key': key_pem(material['root_key'])},
                      {'identity': dict(IDENTITY, path='/tmp/not-owned')}):
        with pytest.raises((ProvisioningError, ValueError)):
            publish(publication, **overrides)
        assert list(publication[1].iterdir()) == []


@pytest.mark.parametrize('overrides', [{'panel_id': 23}, {'identity': dict(IDENTITY, owner='c' * 32)}])
def test_site_receipt_prevents_different_identity_even_for_new_certificate(publication, material, overrides):
    publish(publication)
    before = snapshot(publication[1])
    chain = material['leaf']().public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    with pytest.raises(ProvisioningError):
        publish(publication, fullchain=chain, **overrides)
    assert snapshot(publication[1]) == before


@pytest.mark.parametrize('defect', ['missing-ready', 'changed-key', 'changed-chain', 'changed-ready',
                                     'hardlink-key', 'extra-file', 'missing-owner', 'changed-owner'])
def test_existing_partial_or_changed_version_is_never_repaired(publication, defect):
    version = publish(publication)
    if defect == 'missing-ready':
        (version / 'ready.json').unlink()
    elif defect.startswith('changed-'):
        target = {'changed-key': version / 'privkey.pem', 'changed-chain': version / 'fullchain.pem',
                  'changed-ready': version / 'ready.json', 'changed-owner': version.parent / 'owner.json'}[defect]
        target.write_bytes(b'private-marker')
    elif defect == 'hardlink-key':
        os.link(version / 'privkey.pem', publication[1] / 'linked-key')
    elif defect == 'extra-file':
        (version / 'unexpected').write_bytes(b'private-marker')
    else:
        (version.parent / 'owner.json').unlink()
    before = snapshot(publication[1])
    with pytest.raises(ProvisioningError) as error:
        publish(publication)
    assert 'private-marker' not in str(error.value)
    assert snapshot(publication[1]) == before


def test_interrupted_write_leaves_old_version_and_blocks_automatic_repair(publication, material, monkeypatch):
    from ablab import certificate_versions as module
    old = publish(publication)
    old_snapshot = snapshot(old)
    chain = material['leaf']().public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    original = module.exclusive_text

    def interrupt(path, text, *args, **kwargs):
        if path.name == 'privkey.pem':
            raise OSError('secret-marker')
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(module, 'exclusive_text', interrupt)
    with pytest.raises(ProvisioningError) as error:
        publish(publication, fullchain=chain)
    assert 'secret-marker' not in str(error.value)
    unfinished = [p for p in old.parent.iterdir() if p.is_dir() and p != old]
    assert len(unfinished) == 1
    assert (unfinished[0] / 'fullchain.pem').read_bytes() == chain
    assert not (unfinished[0] / 'ready.json').exists()
    before = snapshot(publication[1])
    monkeypatch.setattr(module, 'exclusive_text', original)
    with pytest.raises(ProvisioningError):
        publish(publication, fullchain=chain)
    assert snapshot(publication[1]) == before
    assert snapshot(old) == old_snapshot


def test_pause_before_private_key_write_stops_publication(publication):
    base = publication[1]
    at_pause = []

    def authorize():
        if list(base.rglob('fullchain.pem')):
            at_pause.append(snapshot(base))
            return False
        return True

    with pytest.raises(ProvisioningError):
        publish(publication, authorize=authorize)
    assert at_pause and snapshot(base) == at_pause[0]
    assert not list(base.rglob('privkey.pem')) and not list(base.rglob('ready.json'))


@pytest.mark.parametrize('deny_at', range(2, 11))
def test_revoked_authorization_stops_each_publication_boundary(publication, deny_at):
    base = publication[1]
    checks, stopped = [], []

    def state():
        return ({str(p.relative_to(base)) for p in base.rglob('*')}, snapshot(base))

    def authorize():
        checks.append(True)
        if len(checks) >= deny_at:
            stopped.append(state())
            return False
        return True

    with pytest.raises(ProvisioningError):
        publish(publication, authorize=authorize)
    assert stopped and state() == stopped[0]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permissions require Linux')
@pytest.mark.parametrize('part', ['base', 'version', 'key'])
def test_unprotected_storage_is_rejected_not_chmodded(publication, part):
    version = publish(publication)
    target = {'base': publication[1], 'version': version, 'key': version / 'privkey.pem'}[part]
    target.chmod(0o755 if target.is_dir() else 0o644)
    mode = target.stat().st_mode
    with pytest.raises(ProvisioningError):
        publish(publication)
    assert target.stat().st_mode == mode


def test_symbolic_link_version_is_rejected(publication):
    version = publish(publication)
    moved = version.with_name('retained-version')
    version.rename(moved)
    try:
        version.symlink_to(moved, target_is_directory=True)
    except OSError as error:
        if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
            pytest.skip('Windows symlink privilege unavailable')
        raise
    before = snapshot(moved)
    with pytest.raises(ProvisioningError):
        publish(publication)
    assert snapshot(moved) == before
