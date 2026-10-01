import json
import subprocess

import pytest

from ablab.provisioning import ProvisioningError


IDENTITY = {'site_id': 'a' * 32, 'domain': 'new.example.com',
            'path': '/www/wwwroot/ab-lab-sites/' + 'a' * 32, 'owner': 'b' * 32}
BASELINE = b'server { listen 80; server_name new.example.com; }\n'


def setup(tmp_path, enabled=True):
    from ablab.nginx_entry import NginxEntry
    configs = tmp_path / 'nginx'
    configs.mkdir()
    config = configs / 'new.example.com.conf'
    config.write_bytes(BASELINE)
    remote = dict(IDENTITY, panel_id=22)
    calls = []

    def inspect(domain, path):
        return dict(remote)

    def runner(args, **kwargs):
        calls.append(args)
        assert kwargs['timeout'] == 15 and kwargs['shell'] is False
        return subprocess.CompletedProcess(args, 0, '', '')

    entry = NginxEntry(tmp_path / 'private', inspect, config_dir=configs, runner=runner, writes_enabled=enabled)
    return entry, config, remote, calls


def test_owned_new_config_is_backed_up_and_checked_before_reload(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)
    entry.configure(IDENTITY, 22)
    text = config.read_text()
    assert 'server_name new.example.com;' in text
    assert 'proxy_pass http://127.0.0.1:8766;' in text
    assert 'proxy_set_header Host new.example.com;' in text
    assert 'proxy_set_header X-Real-IP $remote_addr;' in text
    assert 'proxy_set_header X-Forwarded-Proto $scheme;' in text
    assert 'proxy_set_header X-Forwarded-For "";' in text
    assert 'location ^~ /.well-known/acme-challenge/' in text
    assert 'root /www/wwwroot/ab-lab-sites/' + 'a' * 32 + ';' in text
    assert IDENTITY['owner'] not in text
    assert calls == [['/www/server/nginx/sbin/nginx', '-t'], ['/www/server/nginx/sbin/nginx', '-t'],
                     ['/www/server/nginx/sbin/nginx', '-s', 'reload']]
    backup = json.loads((tmp_path / 'private' / (IDENTITY['site_id'] + '.json')).read_text())
    assert backup['baseline'] == BASELINE.decode()


def test_idempotent_retry_never_overwrites_saved_original(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)
    entry.configure(IDENTITY, 22)
    before = config.read_bytes()
    entry.configure(IDENTITY, 22)
    assert config.read_bytes() == before
    assert sum(cmd[-1] == 'reload' for cmd in calls) == 2
    backup = json.loads((tmp_path / 'private' / (IDENTITY['site_id'] + '.json')).read_text())
    assert backup['baseline'] == BASELINE.decode()


def test_panel_stats_include_added_after_capture_does_not_block_owned_site(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    original = ('server {\n    listen 80;\n    server_name new.example.com;\n'
                '    root /www/wwwroot/ab-lab-sites/' + 'a' * 32 + ';\n}\n')
    config.write_bytes(original.encode())
    entry.capture_created(IDENTITY, 22)
    stats_include = '    include ' + (config.parent / 'extension' / 'new.example.com' / '*.conf').as_posix() + ';\n'
    config.write_bytes(original.replace('    root ' + IDENTITY['path'] + ';\n',
                                        '    root ' + IDENTITY['path'] + ';\n' + stats_include).encode())

    entry.configure(IDENTITY, 22)

    assert 'proxy_pass http://127.0.0.1:8766;' in config.read_text()
    assert calls[-1][-1] == 'reload'
    assert json.loads((entry.root / (IDENTITY['site_id'] + '.json')).read_text())['baseline'] == original


@pytest.mark.parametrize('content', [b'#' + b'\\' * 135193 + b'\n', b'#' + b'x' * (256 * 1024 - 2) + b'\n'],
                         ids=['escaped', 'at-limit'])
def test_accepted_large_baseline_receipt_can_be_read_back(tmp_path, content):
    entry, config, _, calls = setup(tmp_path)
    config.write_bytes(content)
    entry.capture_created(IDENTITY, 22)
    entry.configure(IDENTITY, 22)
    assert b'proxy_pass' in config.read_bytes()
    assert calls[-1][-1] == 'reload'
    backup = json.loads((entry.root / (IDENTITY['site_id'] + '.json')).read_text())
    assert backup['baseline'].encode() == content


def test_invalid_new_config_restores_only_its_original_without_reload(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)

    def fail_second(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if len(calls) == 2 else 0, '', 'private diagnostics')

    entry.runner = fail_second
    with pytest.raises(ProvisioningError) as error:
        entry.configure(IDENTITY, 22)
    assert config.read_bytes() == BASELINE
    assert not any('reload' in cmd for cmd in calls)
    assert 'private diagnostics' not in str(error.value)


@pytest.mark.parametrize('crash', [False, True])
def test_unknown_replacement_blocks_retry_without_losing_either_version(tmp_path, monkeypatch, crash):
    from ablab import nginx_entry
    entry, config, _, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)
    original = nginx_entry.replace_preserving

    def fail(staged, target):
        if crash:
            original(staged, target)
            raise KeyboardInterrupt()
        raise OSError('unsupported filesystem')

    monkeypatch.setattr(nginx_entry, 'replace_preserving', fail)
    with pytest.raises(KeyboardInterrupt if crash else ProvisioningError):
        entry.configure(IDENTITY, 22)
    monkeypatch.setattr(nginx_entry, 'replace_preserving', original)
    before = config.read_bytes()
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert config.read_bytes() == before
    assert any(p.read_bytes() == BASELINE for p in tmp_path.rglob('*') if p.is_file())
    assert not any('reload' in cmd for cmd in calls)


def test_external_edit_after_backup_is_preserved(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)
    config.write_bytes(b'operator changed this')
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert config.read_bytes() == b'operator changed this'
    assert calls == []


def test_ownership_change_prevents_write(tmp_path):
    entry, config, remote, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)
    remote['owner'] = 'c' * 32
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert config.read_bytes() == BASELINE and calls == []


def test_configuration_without_creation_receipt_is_rejected(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert config.read_bytes() == BASELINE and calls == []


def test_nginx_global_failure_leaves_site_and_other_configs_untouched(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    other = config.with_name('other.example.com.conf')
    other.write_bytes(b'existing other site')
    entry.capture_created(IDENTITY, 22)

    def fail(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, '', '')

    entry.runner = fail
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert config.read_bytes() == BASELINE
    assert other.read_bytes() == b'existing other site'
    assert len(calls) == 1


def test_reload_failure_retains_recoverable_new_config_and_backup(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)

    def fail_reload(args, **kwargs):
        calls.append(args)
        if 'reload' in args:
            raise subprocess.TimeoutExpired(args, 15)
        return subprocess.CompletedProcess(args, 0, '', '')

    entry.runner = fail_reload
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert b'proxy_pass' in config.read_bytes()
    assert (tmp_path / 'private' / (IDENTITY['site_id'] + '.json')).exists()


def test_write_gate_blocks_before_backup_or_subprocess(tmp_path):
    entry, config, _, calls = setup(tmp_path, enabled=False)
    with pytest.raises(ProvisioningError):
        entry.capture_created(IDENTITY, 22)
    assert config.read_bytes() == BASELINE and calls == []
    assert list((tmp_path / 'private').iterdir()) == []


def test_manual_edit_during_validation_is_not_overwritten_by_rollback(tmp_path):
    entry, config, _, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)

    def concurrent_edit(args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            config.write_bytes(b'operator edit during validation')
            return subprocess.CompletedProcess(args, 1, '', '')
        return subprocess.CompletedProcess(args, 0, '', '')

    entry.runner = concurrent_edit
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert config.read_bytes() == b'operator edit during validation'
    assert not any('reload' in cmd for cmd in calls)


def test_changed_owner_during_failed_validation_prevents_rollback(tmp_path):
    entry, config, remote, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)

    def change_owner(args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            remote['owner'] = 'c' * 32
            return subprocess.CompletedProcess(args, 1, '', '')
        return subprocess.CompletedProcess(args, 0, '', '')

    entry.runner = change_owner
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert b'proxy_pass' in config.read_bytes()
    assert not any('reload' in cmd for cmd in calls)


def test_parent_traversal_cannot_hide_private_config_overlap(tmp_path):
    from ablab.nginx_entry import NginxEntry
    configs = tmp_path / 'nginx'
    configs.mkdir()
    (tmp_path / 'other').mkdir()
    with pytest.raises((ValueError, ProvisioningError)):
        NginxEntry(tmp_path / 'other' / '..' / 'nginx' / 'private',
                   lambda *args: None, config_dir=configs)
    assert not (configs / 'private').exists()


@pytest.mark.parametrize('during_rollback', [False, True])
def test_edit_during_staging_is_retained_and_blocks_reload_and_retry(tmp_path, monkeypatch, during_rollback):
    import os
    entry, config, _, calls = setup(tmp_path)
    entry.capture_created(IDENTITY, 22)
    original_fsync = os.fsync
    edited = False
    operator = b'operator edit made while replacement is staged'

    def race(fd):
        nonlocal edited
        if not edited and (not during_rollback or len(calls) == 2):
            config.write_bytes(operator)
            edited = True
        return original_fsync(fd)

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if during_rollback and len(calls) == 2 else 0, '', '')

    monkeypatch.setattr(os, 'fsync', race)
    entry.runner = runner
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert edited
    # The actual displaced version must survive, not just the earlier receipt.
    assert any(p.read_bytes() == operator for p in tmp_path.rglob('*') if p.is_file())
    with pytest.raises(ProvisioningError):
        entry.configure(IDENTITY, 22)
    assert not any('reload' in cmd for cmd in calls)
