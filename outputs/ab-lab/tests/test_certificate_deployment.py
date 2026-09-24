from pathlib import Path

import pytest

from ablab.provisioning import ProvisioningError, ProvisioningNotStarted
from test_certificates import material
from test_certificate_versions import publication
from test_panel_sites import IDENTITY


DIGEST = 'd' * 64


class Command:
    environment = 'production'

    def __init__(self, events, *, fail=False, after=None):
        self.events, self.fail, self.after = events, fail, after

    def run(self, identity, *, operation, enabled, authorize):
        if self.fail:
            self.events.append(('command', identity, operation, enabled))
            raise ProvisioningError('unknown outcome')
        if authorize() is not True:
            raise ProvisioningError('paused')
        self.events.append(('command', identity, operation, enabled))
        if self.after:
            self.after()


class Versions:
    def __init__(self, events):
        self.events = events

    def publish(self, identity, panel_id, fullchain, private_key, authorize):
        self.events.append(('publish', identity, panel_id, fullchain, private_key))
        if authorize() is not True:
            raise ProvisioningError('paused')
        return Path('/private/production/deployed/ab-' + identity['site_id']) / DIGEST


class TLS:
    def __init__(self, events, certificates):
        self.events, self.certificates = events, certificates

    def configure(self, identity, panel_id, digest, authorize):
        self.events.append(('tls', identity, panel_id, digest))
        if authorize() is not True:
            raise ProvisioningError('paused')
        return True


def deployment(*, enabled=True, command=None, reader=None):
    from ablab.certificate_deployment import CertificateDeployment
    events = []
    versions = Versions(events)
    command = command or Command(events)

    def material(base, environment, identity):
        events.append(('read', base, environment, identity))
        return b'fullchain', b'private-key'

    result = CertificateDeployment(command, versions, TLS(events, versions),
                                   writes_enabled=enabled,
                                   material_reader=reader or material)
    return result, events


def test_issue_runs_once_then_deploys_verified_version_in_order():
    manager, events = deployment()
    assert manager.issue(IDENTITY, 22, lambda: True) == DIGEST
    assert [event[0] for event in events] == ['command', 'read', 'publish', 'tls']
    assert events[0][1:] == (IDENTITY, 'issue', True)
    assert events[1][1:] == (Path('/var/lib/ab-lab-certificates'), 'production', IDENTITY)
    assert events[2][2:] == (22, b'fullchain', b'private-key')
    assert events[3][2:] == (22, DIGEST)


def test_reconcile_existing_material_never_starts_a_second_order():
    manager, events = deployment()
    assert manager.deploy_existing(IDENTITY, 22, lambda: True) == DIGEST
    assert [event[0] for event in events] == ['read', 'publish', 'tls']


@pytest.mark.parametrize('enabled', [False, None, 0, 1, 'yes'])
def test_default_and_truthy_write_flags_never_start_or_read(enabled):
    manager, events = deployment(enabled=enabled)
    with pytest.raises(ProvisioningError):
        manager.issue(IDENTITY, 22, lambda: True)
    with pytest.raises(ProvisioningError):
        manager.deploy_existing(IDENTITY, 22, lambda: True)
    assert events == []


def test_unknown_command_outcome_is_not_reconciled_in_same_call():
    events = []
    manager, actual = deployment(command=Command(events, fail=True))
    manager.command.events = actual
    with pytest.raises(ProvisioningError):
        manager.issue(IDENTITY, 22, lambda: True)
    assert [event[0] for event in actual] == ['command']


def test_pause_after_command_blocks_publication_and_tls():
    allowed = True

    def authorize():
        return allowed

    def pause():
        nonlocal allowed
        allowed = False

    events = []
    manager, actual = deployment(command=Command(events, after=pause))
    manager.command.events = actual

    with pytest.raises(ProvisioningError) as error:
        manager.issue(IDENTITY, 22, authorize)
    assert not isinstance(error.value, ProvisioningNotStarted)
    assert [event[0] for event in actual] == ['command']


def test_reader_failure_never_publishes_or_changes_tls():
    def bad_reader(*args):
        raise ProvisioningError('bad material')

    manager, events = deployment(reader=bad_reader)
    with pytest.raises(ProvisioningError):
        manager.deploy_existing(IDENTITY, 22, lambda: True)
    assert events == []


def test_only_production_command_and_matching_tls_store_are_accepted():
    from ablab.certificate_deployment import CertificateDeployment
    events = []
    versions = Versions(events)
    command = Command(events)
    command.environment = 'staging'
    with pytest.raises(ValueError):
        CertificateDeployment(command, versions, TLS(events, versions))
    command.environment = 'production'
    with pytest.raises(ValueError):
        CertificateDeployment(command, versions, TLS(events, Versions(events)))


def test_real_versions_and_tls_transaction_are_composed_without_external_ca(tmp_path, publication, material):
    from ablab.certificate_deployment import CertificateDeployment
    from ablab.nginx_tls import TlsEntry
    from test_certificates import key_pem, PEM
    from test_nginx_entry import setup as nginx_setup

    store, _, _, _ = publication
    case = tmp_path / 'nginx-case'
    case.mkdir()
    nginx, config, _, nginx_calls = nginx_setup(case)
    nginx.capture_created(IDENTITY, 22)
    nginx.configure(IDENTITY, 22)
    nginx_calls.clear()
    chain = material['leaf']().public_bytes(PEM) + material['intermediate'].public_bytes(PEM)
    events = []
    manager = CertificateDeployment(
        Command(events), store, TlsEntry(nginx, store), writes_enabled=True,
        material_reader=lambda *args: (chain, key_pem(material['key'])))

    digest = manager.issue(IDENTITY, 22, lambda: True)

    assert len(digest) == 64
    assert f'/production/deployed/ab-{IDENTITY["site_id"]}/{digest}/fullchain.pem' in config.read_text()
    assert [command[1:] for command in nginx_calls] == [['-t'], ['-t'], ['-s', 'reload']]
