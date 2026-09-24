import json

import pytest

from ablab.provisioning import ProvisioningError, ProvisioningNotStarted
from test_panel_sites import IDENTITY


DIGEST = 'd' * 64


class Command:
    environment = 'staging'
    account_id = 'c' * 32

    def __init__(self, lineage, *, failures=None, after=None):
        self.lineage = lineage
        self.root = lineage.parents[2]
        self.failures = failures or {}
        self.after = after or (lambda operation: None)
        self.calls = []

    def run(self, identity, *, operation, enabled, authorize):
        assert identity == IDENTITY and enabled is True and authorize() is True
        self.calls.append(operation)
        failure = self.failures.get(operation)
        if operation == 'issue' and failure != 'not-started-no-lineage':
            self.lineage.write_text('managed staging lineage')
        self.after(operation)
        if failure == 'not-started' or failure == 'not-started-no-lineage':
            raise ProvisioningNotStarted('not launched')
        if failure == 'unknown':
            raise ProvisioningError('unknown outcome')


def setup(tmp_path, *, enabled=True, existing=False, failures=None, after=None):
    from ablab.renewal_rehearsal import RenewalRehearsal
    certificates = tmp_path / 'certificates'
    lineage = certificates / 'staging' / 'config' / 'renewal' / (
        'ab-' + IDENTITY['site_id'] + '.conf')
    lineage.parent.mkdir(parents=True)
    if existing:
        lineage.write_text('managed staging lineage')
    state = tmp_path / 'private'
    state.mkdir(mode=0o700)
    command = Command(lineage, failures=failures, after=after)
    audits = []

    def audit(root, identity, account_id, server, *, operation):
        audits.append((root, identity, account_id, server, operation))
        assert lineage.read_text() == 'managed staging lineage'

    rehearsal = RenewalRehearsal(command, state, writes_enabled=enabled, auditor=audit)
    return rehearsal, command, audits, state, lineage


def phases(state):
    return {path.name.rsplit('.', 2)[-2]: json.loads(path.read_text())['phase']
            for path in state.glob('*.json')}


def test_first_rehearsal_issues_staging_lineage_then_runs_one_dry_run(tmp_path):
    rehearsal, command, audits, state, _ = setup(tmp_path)
    assert rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True) is True
    assert command.calls == ['issue', 'dry-run']
    assert [item[-1] for item in audits] == ['dry-run', 'dry-run', 'dry-run']
    assert phases(state) == {'issue-intent': 'issue_intent', 'issued': 'issued',
                             'dry-run-intent': 'dry_run_intent', 'verified': 'verified'}
    assert rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True) is True
    assert command.calls == ['issue', 'dry-run']


def test_existing_audited_staging_lineage_skips_issue(tmp_path):
    rehearsal, command, _, _, _ = setup(tmp_path, existing=True)
    assert rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True) is True
    assert command.calls == ['dry-run']


def test_new_production_digest_requires_new_dry_run_but_reuses_lineage(tmp_path):
    rehearsal, command, _, _, _ = setup(tmp_path, existing=True)
    assert rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True) is True
    assert rehearsal.verify(IDENTITY, 22, 'e' * 64, lambda: True) is True
    assert command.calls == ['dry-run', 'dry-run']


@pytest.mark.parametrize('enabled', [False, None, 0, 1, 'yes'])
def test_default_or_truthy_disabled_state_never_writes_or_launches(tmp_path, enabled):
    rehearsal, command, _, state, _ = setup(tmp_path, enabled=enabled)
    with pytest.raises(ProvisioningNotStarted):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True)
    assert command.calls == [] and list(state.iterdir()) == []


def test_unknown_issue_without_lineage_is_never_retried(tmp_path):
    rehearsal, command, _, state, _ = setup(tmp_path, failures={'issue': 'unknown'})
    with pytest.raises(ProvisioningError):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True)
    # Simulate that no usable lineage was left by the uncertain command.
    command.lineage.unlink()
    with pytest.raises(ProvisioningError):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True)
    assert command.calls == ['issue']
    assert phases(state) == {'issue-intent': 'issue_intent'}


def test_unknown_issue_with_valid_lineage_reconciles_without_second_issue(tmp_path):
    rehearsal, command, _, _, _ = setup(tmp_path, failures={'issue': 'unknown'})
    with pytest.raises(ProvisioningError):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True)
    command.failures.clear()
    assert rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True) is True
    assert command.calls == ['issue', 'dry-run']


def test_unknown_dry_run_is_never_repeated_automatically(tmp_path):
    rehearsal, command, _, state, _ = setup(
        tmp_path, existing=True, failures={'dry-run': 'unknown'})
    with pytest.raises(ProvisioningError):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True)
    command.failures.clear()
    with pytest.raises(ProvisioningError):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True)
    assert command.calls == ['dry-run']
    assert phases(state) == {'issued': 'issued', 'dry-run-intent': 'dry_run_intent'}


@pytest.mark.parametrize('operation', ['issue', 'dry-run'])
def test_known_prelaunch_denial_rolls_back_only_its_intent(tmp_path, operation):
    existing = operation == 'dry-run'
    failure = 'not-started' if existing else 'not-started-no-lineage'
    rehearsal, command, _, state, _ = setup(
        tmp_path, existing=existing, failures={operation: failure})
    with pytest.raises(ProvisioningNotStarted):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True)
    assert operation.replace('-', '_') + '_intent' not in phases(state).values()
    command.failures.clear()
    assert rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True) is True
    expected = ['issue', 'issue', 'dry-run'] if operation == 'issue' else ['dry-run', 'dry-run']
    assert command.calls == expected


def test_pause_after_issue_leaves_reconcilable_intent_and_stops_dry_run(tmp_path):
    allowed = True

    def after(operation):
        nonlocal allowed
        if operation == 'issue':
            allowed = False

    rehearsal, command, _, _, _ = setup(tmp_path, after=after)
    with pytest.raises(ProvisioningError):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: allowed)
    assert command.calls == ['issue']
    allowed = True
    command.after = lambda operation: None
    assert rehearsal.verify(IDENTITY, 22, DIGEST, lambda: allowed) is True
    assert command.calls == ['issue', 'dry-run']


def test_tampered_or_incomplete_verified_receipt_fails_closed(tmp_path):
    rehearsal, _, _, state, _ = setup(tmp_path, existing=True)
    assert rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True) is True
    verified = next(state.glob('*.verified.json'))
    verified.write_text('{}')
    with pytest.raises(ProvisioningError):
        rehearsal.verify(IDENTITY, 22, DIGEST, lambda: True)


def test_only_staging_command_is_accepted(tmp_path):
    from ablab.renewal_rehearsal import RenewalRehearsal
    state = tmp_path / 'private'
    state.mkdir()
    command = type('Command', (), {'environment': 'production', 'account_id': 'c' * 32})()
    with pytest.raises(ValueError):
        RenewalRehearsal(command, state)
