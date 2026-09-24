"""Fixed argv contracts; no Certbot executable or CA is contacted."""
from dataclasses import FrozenInstanceError
import subprocess

import pytest

from test_panel_sites import IDENTITY
from ablab.provisioning import ProvisioningError, ProvisioningNotStarted


@pytest.mark.parametrize('environment,server', [
    ('production', 'https://acme-v02.api.letsencrypt.org/directory'),
    ('staging', 'https://acme-staging-v02.api.letsencrypt.org/directory'),
])
def test_issue_is_one_domain_with_isolated_directories(environment, server):
    from ablab.certbot_command import CertbotCommand
    command = CertbotCommand(environment, 'c' * 32)
    args = command.issue(IDENTITY)
    root = '/var/lib/ab-lab-certificates/' + environment
    assert args[:2] == ('/usr/bin/certbot', 'certonly')
    for flag, expected in {
        '--server': server, '--account': 'c' * 32,
        '--config': root + '/cli.ini', '--config-dir': root + '/config',
        '--work-dir': root + '/work', '--logs-dir': root + '/logs',
        '--cert-name': 'ab-' + 'a' * 32, '--webroot-path': IDENTITY['path'],
        '-d': 'new.example.com',
    }.items():
        assert args.count(flag) == 1
        assert args[args.index(flag) + 1] == expected
    assert '--webroot' in args
    assert '--non-interactive' in args
    assert '--no-directory-hooks' in args
    assert not set(args) & {'--agree-tos', '--nginx', '--standalone', '--force-renewal'}
    with pytest.raises(FrozenInstanceError):
        command.environment = 'other'


@pytest.mark.parametrize('environment,account', [
    ('other', 'c' * 32), ('../staging', 'c' * 32), (None, 'c' * 32),
    ('production', ''), ('production', '--help'), ('production', 'C' * 32),
    ('production', None), ('production', 'c' * 32 + '\n'),
])
def test_invalid_environment_and_account_rejected(environment, account):
    from ablab.certbot_command import CertbotCommand
    with pytest.raises(ValueError):
        CertbotCommand(environment, account)


@pytest.mark.parametrize('patch', [
    {'domain': '--help'}, {'domain': '*.example.com'}, {'path': '/etc'},
    {'site_id': '../escape'}, {'owner': 'invalid'},
])
def test_bad_identity_never_becomes_command(patch):
    from ablab.certbot_command import CertbotCommand
    command = CertbotCommand('staging', 'c' * 32)
    for build in (command.issue, command.renew):
        with pytest.raises(ValueError):
            build(dict(IDENTITY, **patch))


def test_renew_is_scoped_and_dry_run_uses_staging_account_only():
    from ablab.certbot_command import CertbotCommand
    command = CertbotCommand('staging', 'c' * 32)
    normal = command.renew(IDENTITY)
    dry = command.renew(IDENTITY, dry_run=True)
    assert normal[:2] == ('/usr/bin/certbot', 'renew')
    assert normal[normal.index('--cert-name') + 1] == 'ab-' + 'a' * 32
    assert '--force-renewal' not in normal
    assert '--no-random-sleep-on-renew' in normal
    assert '--dry-run' not in normal
    assert dry == normal + ('--dry-run',)
    with pytest.raises(ValueError):
        CertbotCommand('production', 'c' * 32).renew(IDENTITY, dry_run=True)
    for bad in (1, 'true', None):
        with pytest.raises(ValueError):
            command.renew(IDENTITY, dry_run=bad)


@pytest.mark.parametrize('enabled', [False, None, 1, 'true'])
def test_disabled_execution_never_launches(enabled):
    from ablab.certbot_command import CertbotCommand
    calls = []
    with pytest.raises(ProvisioningNotStarted):
        CertbotCommand('staging', 'c' * 32).run(
            IDENTITY, operation='issue', enabled=enabled, authorize=lambda: True,
            runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


@pytest.mark.parametrize('authorized', [False, None, 1, 'true'])
def test_ownership_and_pause_recheck_must_explicitly_authorize(authorized, monkeypatch):
    from ablab.certbot_command import CertbotCommand
    monkeypatch.setattr('ablab.certbot_command.audit_private_config', lambda *a, **kw: None)
    calls = []
    with pytest.raises(ProvisioningNotStarted):
        CertbotCommand('staging', 'c' * 32).run(
            IDENTITY, operation='renew', enabled=True, authorize=lambda: authorized,
            runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


@pytest.mark.parametrize('operation,verb,dry', [
    ('issue', 'certonly', False), ('renew', 'renew', False), ('dry-run', 'renew', True),
])
def test_launch_is_bounded_without_inherited_environment(monkeypatch, operation, verb, dry):
    from ablab.certbot_command import CertbotCommand
    monkeypatch.setenv('HTTPS_PROXY', 'https://secret.example')
    monkeypatch.setenv('PYTHONPATH', '/untrusted')
    events = []

    def audited(root, identity, account, server, *, operation):
        assert root == '/var/lib/ab-lab-certificates/staging'
        assert identity == IDENTITY and account == 'c' * 32
        assert server == 'https://acme-staging-v02.api.letsencrypt.org/directory'
        assert operation in ('issue', 'renew', 'dry-run')
        events.append('audited')

    monkeypatch.setattr('ablab.certbot_command.audit_private_config', audited)

    def authorize():
        events.append('checked')
        return True

    def run(args, **kwargs):
        assert events == ['audited', 'checked']
        events.append('launched')
        assert args[:2] == ('/usr/bin/certbot', verb)
        assert ('--dry-run' in args) is dry
        assert kwargs == dict(shell=False, timeout=180, check=False, umask=0o077,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, cwd='/var/lib/ab-lab-certificates/staging',
                              env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                   'XDG_CONFIG_HOME': '/var/lib/ab-lab-certificates/staging/.config',
                                   'HOME': '/var/lib/ab-lab-certificates/staging'})
        return subprocess.CompletedProcess(args, 0)

    result = CertbotCommand('staging', 'c' * 32).run(
        IDENTITY, operation=operation, enabled=True, authorize=authorize, runner=run)
    assert events == ['audited', 'checked', 'launched']
    assert result is None  # Exit 0 is NOT evidence that a certificate changed.


@pytest.mark.parametrize('failure', ['timeout', 'oserror', 'exit'])
def test_launch_failure_is_redacted_and_never_retried(failure, monkeypatch):
    from ablab.certbot_command import CertbotCommand
    monkeypatch.setattr('ablab.certbot_command.audit_private_config', lambda *a, **kw: None)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 180, output='private-output')
        if failure == 'oserror':
            raise OSError('private-output')
        return subprocess.CompletedProcess(args, 1, stdout='private-output', stderr='private-output')

    with pytest.raises(ProvisioningError) as error:
        CertbotCommand('staging', 'c' * 32).run(
            IDENTITY, operation='issue', enabled=True, authorize=lambda: True, runner=run)
    assert len(calls) == 1
    assert 'private-output' not in str(error.value)
    assert error.value.__suppress_context__


def test_unknown_operation_rejected_before_authorizing_or_launching():
    from ablab.certbot_command import CertbotCommand
    calls = []
    with pytest.raises(ValueError):
        CertbotCommand('staging', 'c' * 32).run(
            IDENTITY, operation='delete', enabled=True,
            authorize=lambda: calls.append('authorize'), runner=lambda *args, **kw: calls.append('run'))
    assert calls == []


def test_existing_global_cli_blocks_launch_even_with_explicit_private_config(tmp_path, monkeypatch):
    from ablab import certbot_command as module
    global_cli = tmp_path / 'cli.ini'
    global_cli.write_text('pre-hook = /some/global/hook\n')
    monkeypatch.setattr(module, 'SYSTEM_CLI', global_cli, raising=False)
    calls = []
    with pytest.raises(ProvisioningNotStarted):
        module.CertbotCommand('staging', 'c' * 32).run(
            IDENTITY, operation='renew', enabled=True, authorize=lambda: True,
            runner=lambda *args, **kwargs: (calls.append(args) or subprocess.CompletedProcess(args, 0)))
    assert calls == []
    assert global_cli.read_text() == 'pre-hook = /some/global/hook\n'


def test_default_home_cli_is_rejected_without_reading_or_removing_it(tmp_path, monkeypatch):
    from ablab import certbot_command as module
    monkeypatch.setattr(module, 'SYSTEM_CLI', tmp_path / 'absent', raising=False)
    cli = tmp_path / '.config' / 'letsencrypt' / 'cli.ini'
    cli.parent.mkdir(parents=True)
    cli.write_text('installer = nginx\n')
    with pytest.raises(ProvisioningError):
        module.check_default_configs(tmp_path)
    assert cli.read_text() == 'installer = nginx\n'


def test_unreadable_default_config_path_fails_closed(tmp_path, monkeypatch):
    from ablab import certbot_command as module
    from pathlib import Path
    monkeypatch.setattr(module, 'SYSTEM_CLI', tmp_path / 'cli.ini', raising=False)

    def denied(self, **kwargs):
        raise PermissionError('sensitive file')

    monkeypatch.setattr(Path, 'lstat', denied)
    with pytest.raises(ProvisioningError) as error:
        module.check_default_configs(tmp_path)
    assert 'sensitive file' not in str(error.value)
