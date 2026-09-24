from datetime import date
import json
import os
import stat

import pytest

from ablab.provisioning import ProvisioningError, ProvisioningNotStarted


IDENTITY = {
    'site_id': 'a' * 32,
    'domain': 'new.example.com',
    'path': '/www/wwwroot/ab-lab-sites/' + 'a' * 32,
    'owner': 'b' * 32,
}
DIGEST = 'd' * 64


class Command:
    environment = 'production'
    account_id = 'c' * 32

    def __init__(self):
        self.calls = 0
        self.error = None

    def run(self, identity, *, operation, enabled, authorize):
        assert identity == IDENTITY
        assert operation == 'renew' and enabled is True and authorize() is True
        self.calls += 1
        if self.error:
            raise self.error


class Deployment:
    def __init__(self, command, versions):
        self.command, self.versions = command, versions
        self.calls = 0
        self.error = None

    def deploy_existing(self, identity, panel_id, authorize):
        assert identity == IDENTITY and panel_id == 22 and authorize() is True
        self.calls += 1
        if self.error:
            error, self.error = self.error, None
            raise error
        return DIGEST


class Acceptance:
    def __init__(self, versions):
        self.versions = versions
        self.calls = 0
        self.proof = {'https': True, 'route': True, 'renewal': True}

    def verify(self, identity, panel_id, digest, authorize):
        assert identity == IDENTITY and panel_id == 22 and digest == DIGEST
        assert authorize() is True
        self.calls += 1
        return dict(self.proof)


def renewal(tmp_path, day=date(2026, 9, 24)):
    from ablab.production_renewal import ProductionRenewal

    root = tmp_path / 'renewal-production'
    root.mkdir(mode=0o700)
    command, versions = Command(), object()
    deployment = Deployment(command, versions)
    acceptance = Acceptance(versions)
    manager = ProductionRenewal(
        deployment, acceptance, root, writes_enabled=True, today=lambda: day)
    return manager, command, deployment, acceptance, root


def test_success_is_idempotent_for_same_utc_day(tmp_path):
    manager, command, deployment, acceptance, root = renewal(tmp_path)

    assert manager.run(IDENTITY, 22, lambda: True) == {
        'https': True, 'route': True, 'renewal': True}
    assert manager.run(IDENTITY, 22, lambda: True) == {
        'https': True, 'route': True, 'renewal': True}

    assert (command.calls, deployment.calls, acceptance.calls) == (1, 1, 1)
    records = list(root.iterdir())
    assert len(records) == 1
    assert json.loads(records[0].read_text())['cycle'] == '2026-09-24'


def test_unknown_command_result_is_never_retried_automatically(tmp_path):
    manager, command, deployment, _, _ = renewal(tmp_path)
    command.error = ProvisioningError('unknown command result')

    with pytest.raises(ProvisioningError):
        manager.run(IDENTITY, 22, lambda: True)
    command.error = None
    with pytest.raises(ProvisioningError, match='人工'):
        manager.run(IDENTITY, 22, lambda: True)

    assert command.calls == 1
    assert deployment.calls == 0


def test_confirmed_command_recovers_deployment_without_rerunning_certbot(tmp_path):
    manager, command, deployment, acceptance, _ = renewal(tmp_path)
    deployment.error = ProvisioningError('interrupted deployment')

    with pytest.raises(ProvisioningError):
        manager.run(IDENTITY, 22, lambda: True)
    assert manager.run(IDENTITY, 22, lambda: True)['renewal'] is True

    assert command.calls == 1
    assert deployment.calls == 2
    assert acceptance.calls == 1


def test_definite_prelaunch_refusal_rolls_back_intent(tmp_path):
    manager, command, deployment, _, _ = renewal(tmp_path)
    command.error = ProvisioningNotStarted('not launched')

    with pytest.raises(ProvisioningNotStarted):
        manager.run(IDENTITY, 22, lambda: True)
    command.error = None
    assert manager.run(IDENTITY, 22, lambda: True)['https'] is True

    assert command.calls == 2
    assert deployment.calls == 1


def test_new_utc_day_runs_a_new_scoped_renewal(tmp_path):
    manager, command, deployment, acceptance, root = renewal(tmp_path)
    manager.run(IDENTITY, 22, lambda: True)

    from ablab.production_renewal import ProductionRenewal
    next_day = ProductionRenewal(
        deployment, acceptance, root, writes_enabled=True,
        today=lambda: date(2026, 9, 25))
    assert next_day.run(IDENTITY, 22, lambda: True)['route'] is True
    assert command.calls == 2
    assert {path.name for path in root.iterdir()} == {
        f"{IDENTITY['site_id']}.last.json"}


def test_uncertain_final_replace_recovers_without_repeating_external_work(
        tmp_path, monkeypatch):
    manager, command, deployment, acceptance, root = renewal(tmp_path)
    manager.run(IDENTITY, 22, lambda: True)

    from ablab import production_renewal
    next_day = production_renewal.ProductionRenewal(
        deployment, acceptance, root, writes_enabled=True,
        today=lambda: date(2026, 9, 25))
    real_replace = os.replace

    def uncertain_replace(source, destination):
        real_replace(source, destination)
        raise OSError('result unknown')

    monkeypatch.setattr(production_renewal.os, 'replace', uncertain_replace)
    with pytest.raises(ProvisioningError, match='结果不明确'):
        next_day.run(IDENTITY, 22, lambda: True)
    monkeypatch.setattr(production_renewal.os, 'replace', real_replace)

    assert next_day.run(IDENTITY, 22, lambda: True)['renewal'] is True
    assert (command.calls, deployment.calls, acceptance.calls) == (2, 2, 2)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission assertion')
def test_replaced_final_record_remains_private(tmp_path):
    manager, _, deployment, acceptance, root = renewal(tmp_path)
    manager.run(IDENTITY, 22, lambda: True)

    from ablab.production_renewal import ProductionRenewal
    next_day = ProductionRenewal(
        deployment, acceptance, root, writes_enabled=True,
        today=lambda: date(2026, 9, 25))
    next_day.run(IDENTITY, 22, lambda: True)

    record = root / f"{IDENTITY['site_id']}.last.json"
    assert stat.S_IMODE(record.stat().st_mode) == 0o600


@pytest.mark.parametrize(('field', 'invalid'), [
    ('cycle', '0000-00-00'),
    ('version', 'z' * 64),
])
def test_malformed_final_record_is_rejected_before_running_command(
        tmp_path, field, invalid):
    manager, command, _, _, root = renewal(tmp_path)
    manager.run(IDENTITY, 22, lambda: True)
    record = root / f"{IDENTITY['site_id']}.last.json"
    value = json.loads(record.read_text())
    value[field] = invalid
    record.write_text(json.dumps(value), encoding='ascii')
    record.chmod(0o600)

    with pytest.raises(ProvisioningError, match='最终记录'):
        manager.run(IDENTITY, 22, lambda: True)

    assert command.calls == 1


def test_new_day_recovery_keeps_current_intent_separate_from_previous_success(tmp_path):
    manager, command, deployment, acceptance, root = renewal(tmp_path)
    manager.run(IDENTITY, 22, lambda: True)
    deployment.error = ProvisioningError('interrupted new-day deployment')

    from ablab.production_renewal import ProductionRenewal
    next_day = ProductionRenewal(
        deployment, acceptance, root, writes_enabled=True,
        today=lambda: date(2026, 9, 25))
    with pytest.raises(ProvisioningError):
        next_day.run(IDENTITY, 22, lambda: True)
    assert next_day.run(IDENTITY, 22, lambda: True)['https'] is True

    assert command.calls == 2
    assert deployment.calls == 3


def test_invalid_component_graph_and_truthy_proof_are_rejected(tmp_path):
    from ablab.production_renewal import ProductionRenewal

    manager, _, deployment, acceptance, root = renewal(tmp_path)
    acceptance.versions = object()
    with pytest.raises(ValueError):
        ProductionRenewal(deployment, acceptance, root)

    acceptance.versions = deployment.versions
    command = deployment.command
    command.account_id = 'not-an-account'
    with pytest.raises(ValueError):
        ProductionRenewal(deployment, acceptance, root)
    command.account_id = 'c' * 32

    acceptance.proof['renewal'] = 1
    with pytest.raises(ProvisioningError):
        manager.run(IDENTITY, 22, lambda: True)
