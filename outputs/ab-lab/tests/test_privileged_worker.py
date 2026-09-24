import json
import os
from pathlib import Path

import pytest


def config(tmp_path, **overrides):
    value = {
        'schema': 1,
        'enabled': True,
        'server_ip': '8.8.8.8',
        'data_dir': str(tmp_path / 'data'),
        'registry_dir': str(tmp_path / 'registry'),
        'panel_address': 'http://127.0.0.1:34734',
        'panel_config': str(tmp_path / 'panel-api.json'),
        'production_account_id': 'a' * 32,
        'staging_account_id': 'b' * 32,
        'production_roots': str(tmp_path / 'production-roots.pem'),
        'staging_roots': str(tmp_path / 'staging-roots.pem'),
    }
    value.update(overrides)
    return value


def write_config(tmp_path, value):
    path = tmp_path / 'worker.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    path.chmod(0o600)
    return path


def write_raw_config(tmp_path, value):
    path = tmp_path / 'worker.json'
    path.write_text(value, encoding='utf-8')
    path.chmod(0o600)
    return path


def test_private_config_requires_exact_schema_and_literal_enable(tmp_path):
    from ablab.privileged_worker import load_config

    loaded = load_config(write_config(tmp_path, config(tmp_path)))
    assert loaded.enabled is True
    assert loaded.panel_address == 'http://127.0.0.1:34734'

    for changes in ({'extra': 'rejected'}, {'schema': True}, {'enabled': 1}, {'enabled': False}):
        with pytest.raises(ValueError):
            load_config(write_config(tmp_path, config(tmp_path, **changes)))


def test_private_config_rejects_duplicate_json_keys(tmp_path):
    from ablab.privileged_worker import load_config

    value = json.dumps(config(tmp_path))
    ambiguous = value.replace('"enabled": true', '"enabled": false, "enabled": true')
    with pytest.raises(ValueError):
        load_config(write_raw_config(tmp_path, ambiguous))


@pytest.mark.parametrize('field,value', [
    ('panel_address', 'https://panel.example.com'),
    ('panel_address', 'http://127.0.0.1:34734/path'),
    ('panel_address', 'http://localhost:34734'),
    ('production_account_id', 'not-an-account'),
    ('staging_account_id', 'A' * 32),
    ('server_ip', '127.0.0.1'),
    ('server_ip', 134744072),
])
def test_private_config_rejects_remote_panel_bad_accounts_and_nonpublic_ip(tmp_path, field, value):
    from ablab.privileged_worker import load_config

    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, config(tmp_path, **{field: value})))


@pytest.mark.parametrize('field', [
    'data_dir', 'registry_dir', 'panel_config', 'production_roots', 'staging_roots',
])
def test_private_config_requires_absolute_paths_without_parent_traversal(tmp_path, field):
    from ablab.privileged_worker import load_config

    for value in ('relative/path', str(tmp_path / 'safe' / '..' / 'escape')):
        with pytest.raises(ValueError):
            load_config(write_config(tmp_path, config(tmp_path, **{field: value})))


@pytest.mark.skipif(os.name == 'nt', reason='POSIX ownership and mode enforcement')
def test_private_config_rejects_group_readable_file(tmp_path):
    from ablab.privileged_worker import load_config

    path = write_config(tmp_path, config(tmp_path))
    path.chmod(0o640)
    with pytest.raises(ValueError):
        load_config(path)


def test_assembly_wires_one_shared_version_store_and_separate_accounts(tmp_path, monkeypatch):
    from ablab import privileged_worker as module

    settings = module.load_config(write_config(tmp_path, config(tmp_path)))
    events = []

    class Value:
        def __init__(self, kind, *args, **kwargs):
            self.kind, self.args, self.kwargs = kind, args, kwargs
            events.append((kind, args, kwargs, self))

    class Panel(Value):
        @classmethod
        def from_local_config(cls, address, path, *, writes_enabled=False):
            return cls('panel', address, path, writes_enabled=writes_enabled)

    class Managed(Value):
        def __init__(self, api, state_dir, **kwargs):
            super().__init__('managed', api, state_dir, **kwargs)
            self.nginx = object()
            self.bound = None

        def attach_certificates(self, deployment, acceptance):
            self.bound = (deployment, acceptance)

    class Command(Value):
        def __init__(self, environment, account_id):
            super().__init__('command', environment, account_id)
            self.environment, self.account_id = environment, account_id

    class Versions(Value):
        def __init__(self, verifier, base_dir, **kwargs):
            super().__init__('versions', verifier, base_dir, **kwargs)

    class Deployment(Value):
        def __init__(self, command, versions, tls, **kwargs):
            super().__init__('deployment', command, versions, tls, **kwargs)
            self.versions = versions

    class Acceptance(Value):
        def __init__(self, versions, route, **kwargs):
            super().__init__('acceptance', versions, route, **kwargs)
            self.versions = versions

    replacements = {
        'PanelSites': Panel, 'ManagedPanel': Managed, 'CertbotCommand': Command,
        'CertificateVerifier': lambda production, staging: Value('verifier', production, staging),
        'CertificateVersions': Versions, 'TlsEntry': lambda nginx, versions: Value('tls', nginx, versions),
        'CertificateDeployment': Deployment,
        'RenewalRehearsal': lambda command, state, **kw: Value('renewal', command, state, **kw),
        'TlsRouteProbe': lambda ip: Value('route', ip), 'CertificateAcceptance': Acceptance,
        'Registry': lambda data, **kw: Value('registry', data, **kw),
        'ProvisioningWorker': lambda registry, state, ip, panel: Value('worker', registry, state, ip, panel),
    }
    for name, value in replacements.items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, '_read_private', lambda path, limit: (Path(path).name + '\n').encode())
    monkeypatch.setattr(module, 'CERTIFICATE_ROOT', tmp_path / 'certificates')
    monkeypatch.setattr(module, 'WORKER_ROOT', tmp_path / 'worker-private')

    worker = module.assemble(settings)

    managed = next(event[3] for event in events if event[0] == 'managed')
    deployment, acceptance = managed.bound
    assert deployment.versions is acceptance.versions
    commands = [event[1] for event in events if event[0] == 'command']
    assert commands == [('production', 'a' * 32), ('staging', 'b' * 32)]
    assert worker.kind == 'worker'
    assert next(event for event in events if event[0] == 'panel')[2]['writes_enabled'] is True
    assert (tmp_path / 'worker-private').is_dir()
    assert (tmp_path / 'worker-private' / 'renewal').is_dir()


def test_run_pending_uses_registry_snapshot_and_skips_legacy_site():
    from ablab.privileged_worker import run_pending

    class Registry:
        def list(self):
            return [{'id': 'default'}, {'id': 'a' * 32}, {'id': 'b' * 32}]

    class Worker:
        registry = Registry()

        def __init__(self):
            self.calls = []

        def run_once(self, site_id):
            self.calls.append(site_id)

    worker = Worker()
    assert run_pending(worker) == 2
    assert worker.calls == ['a' * 32, 'b' * 32]


def test_load_worker_rejects_non_root_before_reading_config(tmp_path, monkeypatch):
    from ablab import privileged_worker as module

    monkeypatch.setattr(module, '_effective_uid', lambda: 1000)
    with pytest.raises(PermissionError):
        module.load_worker(tmp_path / 'missing-private-config.json')
