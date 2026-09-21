import json
from pathlib import Path
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
