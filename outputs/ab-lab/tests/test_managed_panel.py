from pathlib import Path
import subprocess

import pytest

from ablab.provisioning import ProvisioningError
from test_panel_sites import IDENTITY


def setup(tmp_path, enabled=True):
    from ablab.managed_panel import ManagedPanel
    configs = tmp_path / 'nginx'
    configs.mkdir()
    roots = tmp_path / 'entries'
    roots.mkdir()

    class API:
        writes_enabled = enabled
        object = None
        calls = 0

        def inspect(self, domain, path):
            return dict(self.object) if self.object else None

        def create_static(self, identity):
            self.calls += 1
            self.object = dict(identity, panel_id=22)
            directory = roots / identity['site_id']
            directory.mkdir(exist_ok=True)
            (configs / (identity['domain'] + '.conf')).write_text('server { listen 80; }\n')
            return 22

    api = API()
    runner = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, '', '')
    managed = ManagedPanel(api, tmp_path / 'private', entry_root=roots, config_dir=configs, runner=runner)
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
        managed.certificate(IDENTITY, 22)
    with pytest.raises(ProvisioningError):
        managed.verify(IDENTITY, 22)
    assert api.calls == 0


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
