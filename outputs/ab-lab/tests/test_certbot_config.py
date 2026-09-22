"""Real filesystem audit fixtures; no executable or certificate authority."""
import os
from pathlib import Path
import subprocess

import pytest

from ablab.provisioning import ProvisioningError
from test_panel_sites import IDENTITY


ACCOUNT = 'c' * 32
SERVER = 'https://acme-staging-v02.api.letsencrypt.org/directory'
NAME = 'ab-' + 'a' * 32


@pytest.fixture
def config_tree(tmp_path):
    root = tmp_path / 'certificates' / 'staging'
    root.parent.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    for directory in ('config', 'work', 'logs', 'config/renewal'):
        (root / directory).mkdir(mode=0o700)
    cli = root / 'cli.ini'
    cli.write_text('# Parameters are supplied by the privileged worker.\n', encoding='ascii')
    cli.chmod(0o600)
    live = (root / 'config' / 'live' / NAME).as_posix()
    archive = (root / 'config' / 'archive' / NAME).as_posix()
    renewal = root / 'config' / 'renewal' / (NAME + '.conf')
    renewal.write_text(
        f'version = 5.8.0\narchive_dir = {archive}\n'
        f'cert = {live}/cert.pem\nprivkey = {live}/privkey.pem\n'
        f'chain = {live}/chain.pem\nfullchain = {live}/fullchain.pem\n'
        '[renewalparams]\nauthenticator = webroot\n'
        f'account = {ACCOUNT}\nserver = {SERVER}\nkey_type = ecdsa\n'
        f'webroot_path = {IDENTITY["path"]},\n'
        '[[webroot_map]]\n'
        f'{IDENTITY["domain"]} = {IDENTITY["path"]}\n', encoding='ascii')
    renewal.chmod(0o600)
    return root, cli, renewal


def audit(root, operation='renew'):
    from ablab.certbot_config import audit_private_config
    return audit_private_config(root, IDENTITY, ACCOUNT, SERVER, operation=operation)


@pytest.mark.parametrize('operation', ['renew', 'dry-run'])
def test_scoped_webroot_config_passes_without_mutation(config_tree, operation):
    root, cli, renewal = config_tree
    before = (cli.read_bytes(), renewal.read_bytes())
    assert audit(root, operation) is None
    assert before == (cli.read_bytes(), renewal.read_bytes())


@pytest.mark.parametrize('extra', [
    'pre_hook = /tmp/do-not-run', 'post_hook = /tmp/do-not-run',
    'renew_hook = /tmp/do-not-run', 'deploy_hook = /tmp/do-not-run',
    'installer = nginx', 'installer = None', 'manual_auth_hook = /tmp/do-not-run',
    'allow_subset_of_names = True', 'http01_port = 8000', 'unknown = value',
    'config_dir = /etc/letsencrypt', 'work_dir = /tmp/foreign',
    'logs_dir = /tmp/foreign', 'webroot_map = /tmp/foreign',
    'authenticator = standalone', 'account = ' + 'd' * 32,
    'server = https://untrusted.example/directory',
])
def test_foreign_and_executable_options_never_pass_or_get_repaired(config_tree, extra):
    root, _, renewal = config_tree
    text = renewal.read_text().replace('[[webroot_map]]', extra + '\n[[webroot_map]]')
    renewal.write_text(text)
    before = renewal.read_bytes()
    with pytest.raises(ProvisioningError):
        audit(root)
    assert renewal.read_bytes() == before


@pytest.mark.parametrize('old,new', [
    ('authenticator = webroot', 'authenticator = standalone'),
    ('account = ' + ACCOUNT, 'account = ' + 'd' * 32),
    ('server = ' + SERVER, 'server = https://acme-v02.api.letsencrypt.org/directory'),
    ('key_type = ecdsa', 'key_type = unknown'),
    ('new.example.com = ', 'other.example.com = '),
    ('webroot_path = ', 'webroot_path = /other,'),
    ('/fullchain.pem', '/../../other/fullchain.pem'),
    ('/archive/' + NAME, '/archive/ab-' + 'b' * 32),
    ('version = 5.8.0', 'version = invalid'),
    ('[renewalparams]', '[DEFAULT]'),
    ('[[webroot_map]]', '[webroot_map]'),
    ('authenticator = webroot', 'authenticator = "webroot"'),
    ('authenticator = webroot', 'authenticator = %(plugin)s'),
    ('authenticator = webroot', 'authenticator = webroot # comment'),
])
def test_wrong_identity_paths_or_ambiguous_syntax_rejected(config_tree, old, new):
    root, _, renewal = config_tree
    assert old in renewal.read_text()
    renewal.write_text(renewal.read_text().replace(old, new))
    with pytest.raises(ProvisioningError):
        audit(root)


@pytest.mark.parametrize('extra', [
    'other.example.com = /www/other\n', '[renewalparams]\n',
    'new.example.com = /other\n', '[[[nested]]]\n',
    '[acme_renewal_info]\nari_retry_after = not-a-time\n',
    '[acme_renewal_info]\nari_retry_after = 2026-09-22T08:10:00-01:00\n',
    '[acme_renewal_info]\nari_retry_after = 2026-09-22T08:10:00.123\n',
    '[acme_renewal_info]\npre_hook = /tmp/hook\n',
])
def test_extra_domains_sections_and_ari_data_rejected(config_tree, extra):
    root, _, renewal = config_tree
    renewal.write_text(renewal.read_text() + extra)
    with pytest.raises(ProvisioningError):
        audit(root)


def test_fixed_directory_parameters_and_ari_timestamp_are_accepted(config_tree):
    root, _, renewal = config_tree
    extra = ''.join(f'{key}_dir = {(root / name).as_posix()}\n'
                    for key, name in [('config', 'config'), ('work', 'work'), ('logs', 'logs')])
    renewal.write_text(renewal.read_text().replace('[[webroot_map]]', extra + '[[webroot_map]]')
                       + '[acme_renewal_info]\nari_retry_after = 2026-09-22T08:10:00\n')
    assert audit(root) is None


@pytest.mark.parametrize('text', [
    'pre-hook = /tmp/hook\n', '[section]\n', 'agree-tos = true\n',
    'config-dir = /etc/letsencrypt\n', '# comment\ninstaller = nginx\n',
])
def test_private_cli_cannot_add_any_options(config_tree, text):
    root, cli, _ = config_tree
    cli.write_text(text)
    with pytest.raises(ProvisioningError):
        audit(root)
    assert cli.read_text() == text


@pytest.mark.parametrize('target', ['cli', 'renewal'])
@pytest.mark.parametrize('separator', ['\r', '\v', '\f', '\0'])
def test_comment_cannot_hide_options_behind_control_characters(config_tree, target, separator):
    root, cli, renewal = config_tree
    path = cli if target == 'cli' else renewal
    path.write_bytes(path.read_bytes() + ('# comment' + separator + 'pre_hook = /tmp/hook\n').encode())
    with pytest.raises(ProvisioningError):
        audit(root)


def test_normal_crlf_config_is_supported(config_tree):
    root, cli, renewal = config_tree
    for path in (cli, renewal):
        path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    assert audit(root) is None


def test_empty_cli_supplies_no_options_but_empty_renewal_is_invalid(config_tree):
    root, cli, renewal = config_tree
    cli.write_bytes(b'')
    assert audit(root) is None
    renewal.write_bytes(b'')
    with pytest.raises(ProvisioningError):
        audit(root)


@pytest.mark.parametrize('key_type,option,accepted', [
    ('rsa', 'rsa_key_size = 2048', True), ('rsa', 'rsa_key_size = 3072', True),
    ('rsa', 'rsa_key_size = 4096', True), ('rsa', 'rsa_key_size = 1024', False),
    ('rsa', 'rsa_key_size = invalid', False), ('ecdsa', 'rsa_key_size = 2048', False),
    ('ecdsa', 'elliptic_curve = secp256r1', True),
    ('ecdsa', 'elliptic_curve = secp384r1', True),
    ('ecdsa', 'elliptic_curve = secp192r1', False),
    ('rsa', 'elliptic_curve = secp256r1', False),
])
def test_key_metadata_is_validated(config_tree, key_type, option, accepted):
    root, _, renewal = config_tree
    renewal.write_text(renewal.read_text().replace('key_type = ecdsa',
                                                   f'key_type = {key_type}\n{option}'))
    if accepted:
        assert audit(root) is None
    else:
        with pytest.raises(ProvisioningError):
            audit(root)


def test_unfinished_certbot_rewrite_blocks_launch_without_cleanup(config_tree):
    root, _, renewal = config_tree
    pending = renewal.with_name(renewal.name + '.new')
    pending.write_text('preserve interrupted write')
    with pytest.raises(ProvisioningError):
        audit(root)
    assert pending.read_text() == 'preserve interrupted write'


def test_issue_requires_new_target_but_not_empty_shared_environment(config_tree):
    root, _, renewal = config_tree
    with pytest.raises(ProvisioningError):
        audit(root, 'issue')
    renewal.rename(renewal.with_name('ab-' + 'b' * 32 + '.conf'))
    assert audit(root, 'issue') is None
    target = root / 'config' / 'live' / NAME
    target.mkdir(parents=True, mode=0o700)
    with pytest.raises(ProvisioningError):
        audit(root, 'issue')


@pytest.mark.parametrize('entry', ['cli.ini', 'config/renewal/' + NAME + '.conf', 'work'])
def test_missing_files_or_directories_fail_closed(config_tree, entry):
    root, _, _ = config_tree
    path = root / entry
    path.rename(path.with_name(path.name + '.saved'))
    with pytest.raises(ProvisioningError):
        audit(root)


@pytest.mark.parametrize('entry', ['cli.ini', 'config/renewal/' + NAME + '.conf'])
def test_hardlinked_config_rejected(config_tree, tmp_path, entry):
    root, _, _ = config_tree
    os.link(root / entry, tmp_path / 'second-link')
    with pytest.raises(ProvisioningError):
        audit(root)


@pytest.mark.parametrize('entry', ['cli.ini', 'config/renewal/' + NAME + '.conf', 'work'])
def test_symlinked_config_or_parent_rejected(config_tree, entry):
    root, _, _ = config_tree
    path = root / entry
    saved = path.with_name(path.name + '.saved')
    path.rename(saved)
    try:
        path.symlink_to(saved, target_is_directory=saved.is_dir())
    except OSError as error:
        pytest.skip(f'symlink creation unavailable: {error}')
    with pytest.raises(ProvisioningError):
        audit(root)


@pytest.mark.parametrize('content', [b'x' * 65537, b'\xff\xfe', b'\0'],
                         ids=['oversized', 'invalid-encoding', 'nul'])
def test_oversized_or_invalid_config_is_redacted(config_tree, content):
    root, _, renewal = config_tree
    renewal.write_bytes(content)
    with pytest.raises(ProvisioningError) as error:
        audit(root)
    assert error.value.__suppress_context__


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode enforcement')
@pytest.mark.parametrize('entry,mode', [('cli.ini', 0o644), ('work', 0o777),
                                       ('config/renewal/' + NAME + '.conf', 0o644)])
def test_unsafe_posix_permissions_rejected(config_tree, entry, mode):
    root, _, _ = config_tree
    (root / entry).chmod(mode)
    with pytest.raises(ProvisioningError):
        audit(root)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX ownership enforcement')
def test_foreign_file_owner_rejected(config_tree, monkeypatch):
    root, _, renewal = config_tree
    original = Path.lstat

    def foreign_owner(path, **kwargs):
        info = original(path, **kwargs)
        if path == renewal:
            values = list(info)
            values[4] = info.st_uid + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, 'lstat', foreign_owner)
    with pytest.raises(ProvisioningError):
        audit(root)


def test_real_audit_blocks_process_after_private_config_changes(config_tree, monkeypatch):
    from ablab import certbot_command as command
    from ablab.certbot_config import audit_private_config
    root, cli, _ = config_tree
    monkeypatch.setattr(command, 'SYSTEM_CLI', root / 'absent')
    monkeypatch.setattr(command, 'audit_private_config',
                        lambda ignored, *args, **kwargs: audit_private_config(root, *args, **kwargs))
    launches = []

    def runner(args, **kwargs):
        launches.append(args)
        return subprocess.CompletedProcess(args, 0)

    certbot = command.CertbotCommand('staging', ACCOUNT)
    certbot.run(IDENTITY, operation='renew', enabled=True, authorize=lambda: True, runner=runner)
    assert len(launches) == 1
    cli.write_text('pre-hook = /tmp/do-not-run\n')
    with pytest.raises(ProvisioningError):
        certbot.run(IDENTITY, operation='renew', enabled=True, authorize=lambda: True, runner=runner)
    assert len(launches) == 1
