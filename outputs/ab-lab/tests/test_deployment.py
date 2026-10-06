import json
from pathlib import Path
import subprocess
import sys
import pytest
from deploy.configure import generate


def test_generated_deployment_is_private_and_does_not_edit_nginx(tmp_path):
    generate(tmp_path, 'admin.lab.example', 'lab.example')
    config = json.loads((tmp_path / 'config.json').read_text())
    assert config['admin_origin'] == 'https://admin.lab.example'
    assert config['data_dir'] == '/var/lib/ab-lab'
    for service, port in [('admin', 8765), ('target', 8766)]:
        unit = (tmp_path / f'ab-lab-{service}.service').read_text()
        assert 'User=ab-lab' in unit and 'ProtectSystem=strict' in unit
        assert f'--service {service} --config /etc/ab-lab/config.json' in unit
        proxy = (tmp_path / f'nginx-{service}.conf').read_text()
        assert f'proxy_pass http://127.0.0.1:{port};' in proxy
        assert 'proxy_set_header X-Real-IP $remote_addr;' in proxy
        assert 'proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;' in proxy
        assert 'proxy_set_header X-Forwarded-Proto $scheme;' in proxy
        assert 'location ^~ /' in proxy and 'proxy_cache off;' in proxy
        assert 'ssl_certificate' not in proxy and 'listen ' not in proxy
    assert len(list(tmp_path.iterdir())) == 5


def test_generator_rejects_injection_and_overwriting(tmp_path):
    for name in ['evil;return 200;', 'https://a.test', 'same.test/path', '../etc', 'a.test\nfoo']:
        with pytest.raises(ValueError):
            generate(tmp_path, name, 'lab.example')
        assert list(tmp_path.iterdir()) == []
    generate(tmp_path, 'admin.lab.example', 'lab.example')
    before = (tmp_path / 'config.json').read_bytes()
    with pytest.raises(FileExistsError):
        generate(tmp_path, 'other.lab.example', 'lab.example')
    assert (tmp_path / 'config.json').read_bytes() == before


def test_installer_provisions_readable_venv_before_service_user_runs():
    script = (Path(__file__).resolve().parents[1] / 'deploy' / 'install.sh').read_text()
    assert script.index('pip install') < script.index('chmod -R u=rwX,go=rX /opt/ab-lab') < script.index('runuser -u ab-lab --')
    assert 'nginx -s' not in script and 'systemctl restart nginx' not in script
    assert 'rm -rf' not in script


def test_privileged_service_is_packaged_but_not_installed_or_enabled():
    root = Path(__file__).resolve().parents[1]
    unit = (root / 'deploy' / 'ab-lab-provision.service').read_text()
    assert 'User=root' in unit and 'Group=root' in unit
    assert 'After=network-online.target ab-lab-admin.service ab-lab-target.service' in unit
    assert 'Wants=network-online.target ab-lab-target.service' in unit
    assert '--service provision --private-config /etc/ab-lab/provision.json' in unit
    assert 'UMask=0077' in unit and 'ProtectSystem=strict' in unit
    assert 'StateDirectory=ab-lab-provision ab-lab-certificates' in unit
    assert 'StateDirectoryMode=0700' in unit
    assert 'ReadWritePaths=/var/lib/ab-lab /www/wwwroot ' in unit
    installer = (root / 'deploy' / 'install.sh').read_text()
    assert 'ab-lab-provision.service' not in installer
    assert 'systemctl enable ab-lab-provision' not in installer

    renew = (root / 'deploy' / 'ab-lab-renew.service').read_text()
    timer = (root / 'deploy' / 'ab-lab-renew.timer').read_text()
    assert 'Type=oneshot' in renew and 'User=root' in renew
    assert '--service renew --private-config /etc/ab-lab/provision.json' in renew
    assert 'StateDirectory=ab-lab-provision ab-lab-certificates' in renew
    assert 'OnCalendar=daily' in timer and 'Persistent=true' in timer
    assert 'RandomizedDelaySec=' in timer and 'Unit=ab-lab-renew.service' in timer
    assert 'ab-lab-renew.service' not in installer
    assert 'ab-lab-renew.timer' not in installer
    origin = (root / 'deploy' / 'ab-lab-origin.service').read_text()
    assert 'User=root' in origin and '--service origin --config /etc/ab-lab/config.json' in origin
    assert 'ReadWritePaths=/var/lib/ab-lab /var/lib/ab-lab-origin /var/lib/ab-lab-certificates ' in origin
    assert 'ab-lab-origin.service' not in installer
    assert 'systemctl enable ab-lab-origin' not in installer


@pytest.mark.parametrize('arguments', [
    ['--service', 'provision'],
    ['--service', 'renew'],
    ['--service', 'origin'],
    ['--service', 'origin', '--private-config', 'private.json'],
    ['--service', 'origin', '--config', 'public.json', '--private-config', 'private.json'],
    ['--service', 'admin', '--private-config', 'private.json'],
    ['--service', 'provision', '--private-config', 'private.json', '--config', 'public.json'],
    ['--service', 'renew', '--private-config', 'private.json', '--config', 'public.json'],
])
def test_service_cli_rejects_missing_or_misplaced_private_config(arguments):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / 'run.py'), *arguments],
        cwd=root, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 2
    assert 'private-config' in result.stderr or '不能' in result.stderr
