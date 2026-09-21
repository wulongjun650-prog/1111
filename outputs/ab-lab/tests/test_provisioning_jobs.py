"""Real registry/checkpoint recovery; only the remote panel and DNS are doubles."""
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from ablab.provisioning import ProvisioningError
from ablab.sites import Registry


class DNS:
    def __init__(self, ready=True):
        self.ready = ready

    def check(self, domain, address):
        assert domain == 'new.example.com'
        assert address == '8.8.8.8'
        if not self.ready:
            raise ProvisioningError('DNS not ready')


class Panel:
    def __init__(self):
        self.object = None
        self.calls = []
        self.after_create = lambda: None
        self.proof = {'https': True, 'route': True, 'renewal': True}
        self.create_error = False
        self.certificate_error = False
        self.foreign_directory = False

    def inspect(self, domain, path):
        if self.foreign_directory:
            raise ProvisioningError('foreign directory')
        return dict(self.object) if self.object else None

    def create(self, identity):
        self.calls.append('create')
        self.object = dict(identity, panel_id=22)
        self.after_create()
        if self.create_error:
            raise TimeoutError('secret-panel-response')

    def configure(self, identity, panel_id):
        assert panel_id == 22 and identity['owner'] == self.object['owner']
        self.calls.append('configure')

    def certificate(self, identity, panel_id):
        assert panel_id == 22 and identity['owner'] == self.object['owner']
        self.calls.append('certificate')
        if self.certificate_error:
            raise TimeoutError('secret-certificate-response')

    def verify(self, identity, panel_id):
        assert panel_id == 22
        self.calls.append('verify')
        return self.proof


def worker(tmp_path, registry, panel, dns=None):
    from ablab.provisioning_jobs import ProvisioningWorker
    return ProvisioningWorker(registry, tmp_path / 'worker-private', '8.8.8.8', panel, dns or DNS())


def setup(tmp_path):
    registry = Registry(tmp_path / 'web-data')
    site = registry.add('new.example.com')
    return registry, site, Panel()


def test_complete_job_requires_verified_remote_ownership_and_all_proofs(tmp_path):
    registry, site, panel = setup(tmp_path)
    worker(tmp_path, registry, panel).run_once(site['id'])
    result = Registry(tmp_path / 'web-data').get(site['id'])
    assert result['stage'] == 'active'
    assert result['panel_id'] == 22
    assert result['managed_path'] == '/www/wwwroot/ab-lab-sites/' + site['id']
    assert panel.calls == ['create', 'configure', 'certificate', 'verify']
    owner = panel.object['owner']
    assert owner not in json.dumps(result)
    assert owner not in json.dumps(registry.events(site['id']))
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert panel.calls == ['create', 'configure', 'certificate', 'verify']


def test_existing_site_is_never_adopted_from_web_registry(tmp_path):
    registry, site, panel = setup(tmp_path)
    path = '/www/wwwroot/ab-lab-sites/' + site['id']
    panel.object = {'panel_id': 22, 'domain': site['domain'], 'path': path, 'site_id': site['id'], 'owner': 'foreign'}
    registry.transition(site['id'], 0, 'creating', panel_id=22, managed_path=path)
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'failed'
    assert panel.calls == []


def test_foreign_directory_blocks_creation(tmp_path):
    registry, site, panel = setup(tmp_path)
    panel.foreign_directory = True
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'failed'
    assert panel.calls == []


def test_dns_failure_prevents_all_panel_writes(tmp_path):
    registry, site, panel = setup(tmp_path)
    worker(tmp_path, registry, panel, DNS(False)).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'waiting_dns'
    assert registry.get(site['id'])['next_attempt'] > 0
    assert panel.calls == []


def test_timeout_after_creation_is_reconciled_after_worker_restart(tmp_path):
    registry, site, panel = setup(tmp_path)
    panel.create_error = True
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'failed'
    assert 'secret-panel-response' not in json.dumps(registry.events(site['id']))
    registry.control(site['id'], 'retry')
    worker(tmp_path, Registry(tmp_path / 'web-data'), panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'active'
    assert panel.calls.count('create') == 1


def test_ambiguous_creation_never_repeats_even_after_manual_retry(tmp_path):
    registry, site, panel = setup(tmp_path)
    panel.create_error = True
    panel.after_create = lambda: setattr(panel, 'object', None)
    worker(tmp_path, registry, panel).run_once(site['id'])
    registry.control(site['id'], 'retry')
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'failed'
    assert panel.calls == ['create']


def test_pause_during_create_stops_later_writes_and_resume_reconciles(tmp_path):
    registry, site, panel = setup(tmp_path)
    panel.after_create = lambda: registry.control(site['id'], 'pause')
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'paused'
    assert panel.calls == ['create']
    registry.control(site['id'], 'retry')
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'active'
    assert panel.calls.count('create') == 1


@pytest.mark.parametrize('field,value', [('owner', 'foreign'), ('path', '/www/wwwroot/existing'), ('domain', 'other.example.com'), ('panel_id', True)])
def test_ownership_rechecked_after_create_before_configuration(tmp_path, field, value):
    registry, site, panel = setup(tmp_path)
    panel.after_create = lambda: panel.object.update({field: value})
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'failed'
    assert panel.calls == ['create']


@pytest.mark.parametrize('field', ['https', 'route', 'renewal'])
@pytest.mark.parametrize('value', [False, 1, 'true'])
def test_only_explicit_boolean_proofs_can_mark_active(tmp_path, field, value):
    registry, site, panel = setup(tmp_path)
    panel.proof[field] = value
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'failed'
    registry.control(site['id'], 'retry')
    panel.proof[field] = True
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'active'
    assert panel.calls.count('certificate') == 1


def test_certificate_timeout_does_not_resubmit_order(tmp_path):
    registry, site, panel = setup(tmp_path)
    panel.certificate_error = True
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'failed'
    registry.control(site['id'], 'retry')
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'active'
    assert panel.calls.count('certificate') == 1
    assert 'secret-certificate-response' not in json.dumps(registry.events(site['id']))


def test_backoff_and_paused_default_sites_are_not_processed(tmp_path):
    registry, site, panel = setup(tmp_path)
    instance = worker(tmp_path, registry, panel)
    registry.transition(site['id'], 0, 'failed', delay=3600)
    instance.run_once(site['id'])
    registry.control(site['id'], 'pause')
    instance.run_once(site['id'])
    instance.run_once('default')
    assert panel.calls == []


def test_two_workers_cannot_create_the_same_site_concurrently(tmp_path):
    registry, site, panel = setup(tmp_path)
    started, release = threading.Event(), threading.Event()
    errors = []
    first = worker(tmp_path, registry, panel)
    second = worker(tmp_path, registry, panel)

    def block():
        started.set()
        assert release.wait(5)

    def run():
        try:
            first.run_once(site['id'])
        except Exception as error:
            errors.append(error)

    panel.after_create = block
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(5)
        assert second.run_once(site['id']) == 'busy'
        # A new interpreter must also respect the lock, not merely another
        # instance in this process. The child cannot reach its None adapter.
        command = '''
import sys
from ablab.sites import Registry
from ablab.provisioning_jobs import ProvisioningWorker
from pathlib import Path
root = Path(sys.argv[1])
worker = ProvisioningWorker(Registry(root / 'web-data'), root / 'worker-private', '8.8.8.8', None)
print(worker.run_once(sys.argv[2]))
'''
        result = subprocess.run([sys.executable, '-c', command, str(tmp_path), site['id']],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=4)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'busy'
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert panel.calls.count('create') == 1
    assert registry.get(site['id'])['stage'] == 'active'


def test_private_checkpoint_directory_cannot_be_web_writable_data(tmp_path):
    from ablab.provisioning_jobs import ProvisioningWorker
    registry, _, panel = setup(tmp_path)
    with pytest.raises(ValueError):
        ProvisioningWorker(registry, registry.root / 'worker', '8.8.8.8', panel, DNS())


def test_process_interruption_keeps_intent_and_releases_lock(tmp_path):
    registry, site, panel = setup(tmp_path)

    def interrupt():
        raise KeyboardInterrupt()

    panel.after_create = interrupt
    with pytest.raises(KeyboardInterrupt):
        worker(tmp_path, registry, panel).run_once(site['id'])
    registry.control(site['id'], 'retry')
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'active'
    assert panel.calls.count('create') == 1


def test_changed_identity_cannot_redirect_a_recovery_job(tmp_path):
    registry, site, panel = setup(tmp_path)
    panel.create_error = True
    worker(tmp_path, registry, panel).run_once(site['id'])
    # Simulate a corrupted/web-modified catalog, not an authorized rename API.
    with registry.connect() as db:
        db.execute('UPDATE sites SET domain=? WHERE id=?', ('other.example.com', site['id']))
    registry.control(site['id'], 'retry')

    class MatchingDNS:
        def check(self, domain, address):
            return [address]

    worker(tmp_path, registry, panel, MatchingDNS()).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'failed'
    assert panel.calls == ['create']


@pytest.mark.parametrize('phase', ['create_intent', 'certificate_intent'])
def test_orderly_pause_after_intent_before_request_remains_resumable(tmp_path, monkeypatch, phase):
    registry, site, panel = setup(tmp_path)
    instance = worker(tmp_path, registry, panel)
    save = instance._save

    def pause_after_commit(job):
        save(job)
        if job['phase'] == phase:
            registry.control(site['id'], 'pause')

    # Deterministic scheduling at the real durable commit boundary; persistence
    # and catalog control still execute against real SQLite databases.
    monkeypatch.setattr(instance, '_save', pause_after_commit)
    instance.run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'paused'
    assert panel.calls == ([] if phase == 'create_intent' else ['create', 'configure'])
    registry.control(site['id'], 'retry')
    worker(tmp_path, registry, panel).run_once(site['id'])
    assert registry.get(site['id'])['stage'] == 'active'
    assert panel.calls == ['create', 'configure', 'certificate', 'verify']
