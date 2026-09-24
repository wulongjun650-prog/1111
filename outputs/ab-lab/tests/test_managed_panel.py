from pathlib import Path
import subprocess

import pytest

from ablab.provisioning import ProvisioningError
from test_panel_sites import IDENTITY


def setup(tmp_path, enabled=True, certificate_deployment=None, acceptance=None):
    from ablab.managed_panel import ManagedPanel
    configs = tmp_path / 'nginx'
    configs.mkdir()
    roots = tmp_path / 'entries'
    roots.mkdir()

    class API:
        writes_enabled = enabled
        object = None
        calls = 0
        after_inspect = lambda self: None

        def inspect(self, domain, path):
            result = dict(self.object) if self.object else None
            self.after_inspect()
            return result

        def create_static(self, identity):
            self.calls += 1
            self.object = dict(identity, panel_id=22)
            directory = roots / identity['site_id']
            directory.mkdir(exist_ok=True)
            (configs / (identity['domain'] + '.conf')).write_text('server { listen 80; }\n')
            return 22

    api = API()
    runner = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, '', '')
    managed = ManagedPanel(api, tmp_path / 'private', entry_root=roots, config_dir=configs, runner=runner,
                           certificate_deployment=certificate_deployment, acceptance=acceptance)
    return managed, api, roots, configs


def test_create_then_configure_real_files_without_touching_other_sites(tmp_path):
    managed, api, roots, configs = setup(tmp_path)
    unrelated = configs / 'other.example.com.conf'
    unrelated.write_text('existing other site')
    assert managed.inspect(IDENTITY['domain'], IDENTITY['path']) is None
    managed.create(IDENTITY)
    assert managed.inspect(IDENTITY['domain'], IDENTITY['path']) == dict(IDENTITY, panel_id=22)
    managed.configure(IDENTITY, 22)
    assert (roots / IDENTITY['site_id']).is_dir()
    assert 'proxy_pass' in (configs / 'new.example.com.conf').read_text()
    assert unrelated.read_text() == 'existing other site'
    assert api.calls == 1


@pytest.mark.parametrize('conflict', ['directory', 'file', 'config'])
def test_foreign_disk_objects_block_before_api_create(tmp_path, conflict):
    managed, api, roots, configs = setup(tmp_path)
    if conflict == 'directory':
        (roots / IDENTITY['site_id']).mkdir()
    elif conflict == 'file':
        (roots / IDENTITY['site_id']).write_text('keep')
    else:
        (configs / 'new.example.com.conf').write_text('manual configuration')
    with pytest.raises(ProvisioningError):
        managed.create(IDENTITY)
    assert api.calls == 0


def test_existing_receipt_blocks_new_create_even_when_panel_and_root_missing(tmp_path):
    managed, api, roots, configs = setup(tmp_path)
    managed.create(IDENTITY)
    api.object = None
    (roots / IDENTITY['site_id']).rmdir()
    (configs / 'new.example.com.conf').unlink()
    with pytest.raises(ProvisioningError):
        managed.create(IDENTITY)
    assert api.calls == 1


def test_no_config_capture_after_addsite_timeout(tmp_path):
    managed, api, _, _ = setup(tmp_path)
    original = api.create_static

    def timeout(identity):
        original(identity)
        raise ProvisioningError('unknown create result')

    api.create_static = timeout
    with pytest.raises(ProvisioningError):
        managed.create(IDENTITY)
    assert managed.inspect(IDENTITY['domain'], IDENTITY['path']) == dict(IDENTITY, panel_id=22)
    with pytest.raises(ProvisioningError):
        managed.configure(IDENTITY, 22)
    assert list((tmp_path / 'private').iterdir()) == []


def test_certificate_and_verification_remain_explicitly_unavailable(tmp_path):
    managed, api, _, _ = setup(tmp_path)
    with pytest.raises(ProvisioningError):
        managed.certificate(IDENTITY, 22, lambda: True)
    with pytest.raises(ProvisioningError):
        managed.verify(IDENTITY, 22, lambda: True)
    assert api.calls == 0


def test_certificate_combines_worker_authorization_with_exact_panel_ownership(tmp_path):
    class Deployment:
        calls = []

        def issue(self, identity, panel_id, authorize):
            self.calls.append((identity, panel_id, authorize))
            assert authorize() is True
            return 'd' * 64

    deployment = Deployment()
    managed, api, _, _ = setup(tmp_path, certificate_deployment=deployment)
    managed.create(IDENTITY)
    assert managed.certificate(IDENTITY, 22, lambda: True) == 'd' * 64
    assert deployment.calls[0][:2] == (IDENTITY, 22)
    api.object['owner'] = 'c' * 32
    assert deployment.calls[0][2]() is False


@pytest.mark.parametrize('authorization', [False, None, 0, 1, 'yes'])
def test_certificate_requires_literal_worker_authorization(tmp_path, authorization):
    class Deployment:
        called = False

        def issue(self, identity, panel_id, authorize):
            self.called = True
            assert authorize() is False

    deployment = Deployment()
    managed, _, _, _ = setup(tmp_path, certificate_deployment=deployment)
    managed.certificate(IDENTITY, 22, lambda: authorization)
    assert deployment.called


def test_pause_during_panel_inspection_revokes_certificate_authorization(tmp_path):
    class Deployment:
        def issue(self, identity, panel_id, authorize):
            assert authorize() is False

    allowed = True
    managed, api, _, _ = setup(tmp_path, certificate_deployment=Deployment())
    managed.create(IDENTITY)

    def pause():
        nonlocal allowed
        allowed = False

    api.after_inspect = pause
    managed.certificate(IDENTITY, 22, lambda: allowed)


def test_verification_reconciles_existing_material_but_remains_fail_closed(tmp_path):
    class Deployment:
        calls = []

        def deploy_existing(self, identity, panel_id, authorize):
            self.calls.append((identity, panel_id))
            assert authorize() is True
            return 'd' * 64

    deployment = Deployment()
    managed, _, _, _ = setup(tmp_path, certificate_deployment=deployment)
    managed.create(IDENTITY)
    with pytest.raises(ProvisioningError, match='HTTPS'):
        managed.verify(IDENTITY, 22, lambda: True)
    assert deployment.calls == [(IDENTITY, 22)]


def test_verification_passes_current_version_to_matching_acceptance_store(tmp_path):
    versions = object()

    class Deployment:
        def __init__(self):
            self.versions = versions

        def deploy_existing(self, identity, panel_id, authorize):
            assert authorize() is True
            return 'd' * 64

    class Acceptance:
        def __init__(self):
            self.versions = versions
            self.calls = []

        def verify(self, identity, panel_id, digest, authorize):
            self.calls.append((identity, panel_id, digest))
            assert authorize() is True
            return {'https': True, 'route': True, 'renewal': False}

    acceptance = Acceptance()
    managed, _, _, _ = setup(tmp_path, certificate_deployment=Deployment(),
                             acceptance=acceptance)
    managed.create(IDENTITY)
    assert managed.verify(IDENTITY, 22, lambda: True) == {
        'https': True, 'route': True, 'renewal': False}
    assert acceptance.calls == [(IDENTITY, 22, 'd' * 64)]


def test_acceptance_and_deployment_must_share_the_same_version_store(tmp_path):
    class Deployment:
        versions = object()

    class Acceptance:
        versions = object()

    with pytest.raises(ValueError):
        setup(tmp_path, certificate_deployment=Deployment(), acceptance=Acceptance())


def test_certificate_pipeline_can_be_bound_exactly_once_after_nginx_exists(tmp_path):
    managed, _, _, _ = setup(tmp_path)
    versions = object()
    deployment = type('Deployment', (), {'versions': versions})()
    acceptance = type('Acceptance', (), {'versions': versions})()
    managed.attach_certificates(deployment, acceptance)
    assert managed.certificate_deployment is deployment and managed.acceptance is acceptance
    with pytest.raises(ValueError):
        managed.attach_certificates(deployment, acceptance)


def test_late_certificate_binding_rejects_mismatched_store(tmp_path):
    managed, _, _, _ = setup(tmp_path)
    deployment = type('Deployment', (), {'versions': object()})()
    acceptance = type('Acceptance', (), {'versions': object()})()
    with pytest.raises(ValueError):
        managed.attach_certificates(deployment, acceptance)


def test_certificate_pipeline_properties_cannot_be_replaced_directly(tmp_path):
    managed, _, _, _ = setup(tmp_path)
    with pytest.raises(AttributeError):
        managed.certificate_deployment = object()
    with pytest.raises(AttributeError):
        managed.acceptance = object()


def test_late_binding_rejects_missing_or_none_version_store(tmp_path):
    managed, _, _, _ = setup(tmp_path)
    for deployment, acceptance in [
        (object(), object()),
        (type('Deployment', (), {'versions': None})(),
         type('Acceptance', (), {'versions': None})()),
    ]:
        with pytest.raises(ValueError):
            managed.attach_certificates(deployment, acceptance)


def test_production_renewal_binds_once_to_the_existing_certificate_pipeline(tmp_path):
    managed, _, _, _ = setup(tmp_path)
    versions = object()
    deployment = type('Deployment', (), {'versions': versions})()
    acceptance = type('Acceptance', (), {'versions': versions})()
    renewal = type('Renewal', (), {
        'deployment': deployment, 'acceptance': acceptance,
    })()
    managed.attach_certificates(deployment, acceptance)
    managed.attach_renewal(renewal)
    assert managed.production_renewal is renewal
    with pytest.raises(ValueError):
        managed.attach_renewal(renewal)
    with pytest.raises(AttributeError):
        managed.production_renewal = object()


def test_production_renewal_requires_exact_pipeline_and_live_ownership(tmp_path):
    managed, api, _, _ = setup(tmp_path)
    versions = object()
    deployment = type('Deployment', (), {'versions': versions})()
    acceptance = type('Acceptance', (), {'versions': versions})()

    class Renewal:
        def __init__(self):
            self.deployment, self.acceptance = deployment, acceptance
            self.authorization = None

        def run(self, identity, panel_id, authorize):
            self.authorization = authorize
            assert authorize() is True
            return {'https': True, 'route': True, 'renewal': True}

    renewal = Renewal()
    managed.attach_certificates(deployment, acceptance)
    managed.attach_renewal(renewal)
    managed.create(IDENTITY)
    assert managed.renew(IDENTITY, 22, lambda: True)['renewal'] is True
    api.object['owner'] = 'c' * 32
    assert renewal.authorization() is False

    other = type('Renewal', (), {
        'deployment': object(), 'acceptance': acceptance,
    })()
    second_root = tmp_path / 'second'
    second_root.mkdir()
    second, _, _, _ = setup(second_root)
    second.attach_certificates(deployment, acceptance)
    with pytest.raises(ValueError):
        second.attach_renewal(other)


def test_default_disabled_api_cannot_create_local_directories(tmp_path):
    managed, api, roots, _ = setup(tmp_path, enabled=False)
    with pytest.raises(ProvisioningError):
        managed.create(IDENTITY)
    assert api.calls == 0
    assert not (roots / IDENTITY['site_id']).exists()


def test_private_state_under_public_root_is_rejected_before_mkdir(tmp_path):
    from ablab.managed_panel import ManagedPanel
    from types import SimpleNamespace
    public = tmp_path / 'public'
    state = public / 'private'
    with pytest.raises(ValueError):
        ManagedPanel(SimpleNamespace(writes_enabled=False), state,
                     entry_root=public, config_dir=tmp_path / 'nginx')
    assert not state.exists()
