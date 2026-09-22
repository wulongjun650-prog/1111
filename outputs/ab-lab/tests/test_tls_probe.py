"""Probe subprocess boundary: never start/reload Nginx or use live configuration."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


def module():
    deploy = Path(__file__).resolve().parents[1] / 'deploy'
    sys.path.insert(0, str(deploy))
    try:
        spec = importlib.util.spec_from_file_location('tls_probe', deploy / 'build_tls_probe.py')
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded
    finally:
        sys.path.remove(str(deploy))


def test_nginx_probe_only_checks_explicit_private_configuration(tmp_path):
    check = module().nginx_check
    config = tmp_path / 'nginx.conf'
    config.write_text('events {} http {}')
    seen = []

    def runner(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '', '')

    assert check(tmp_path, config, runner=runner).returncode == 0
    args, kwargs = seen[0]
    assert args == ['/www/server/nginx/sbin/nginx', '-t', '-p', str(tmp_path.resolve()) + '/',
                    '-c', str(config.resolve()), '-e', str(tmp_path.resolve() / 'error.log')]
    assert kwargs['timeout'] == 15 and kwargs['shell'] is False
    assert kwargs['env']['HOME'] == str(tmp_path.resolve())


def test_nginx_probe_rejects_configuration_outside_private_prefix(tmp_path):
    outside = tmp_path / 'outside.conf'
    outside.write_text('events {}')
    prefix = tmp_path / 'isolated'
    prefix.mkdir()
    with pytest.raises(ValueError):
        module().nginx_check(prefix, outside, runner=lambda *args, **kw: pytest.fail('must not run'))


def test_generated_probe_compiles_current_production_classes():
    source = module().build_probe()
    code = compile(source, 'isolated-tls-probe', 'exec')
    assert {'TlsEntry', 'CertificateVersions', 'nginx_check'} <= set(code.co_names)


def test_syntax_probe_replaces_tcp_listeners_with_private_unix_sockets(tmp_path):
    isolate = module().private_listeners
    rendered = isolate('server { listen 80; } server { listen 443 ssl; }', tmp_path)
    assert rendered == (f'server {{ listen unix:{tmp_path.as_posix()}/http.sock; }} '
                        f'server {{ listen unix:{tmp_path.as_posix()}/https.sock ssl; }}')
    with pytest.raises(ValueError): isolate('server { listen 8080; }', tmp_path)
    with pytest.raises(ValueError): isolate('server {}', tmp_path)


@pytest.mark.parametrize('value', [None, b'', b'1234\n'])
def test_probe_pid_allows_nginx_test_empty_file_but_not_process_id(tmp_path, value):
    path = tmp_path / 'nginx.pid'
    if value is not None: path.write_bytes(value)
    assert module().empty_probe_pid(path) is (not value)
