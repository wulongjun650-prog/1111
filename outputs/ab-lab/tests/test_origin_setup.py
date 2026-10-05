import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from ablab.origin_setup import (
    CERT_FAILED, NGINX_FAILED, READY, OriginSetup, acme_worker_source, panel_loopback_address,
)
from ablab.provisioning import ProvisioningError
from ablab.sites import Registry


DOMAIN = 'new.example.com'
OTHER = 'server { listen 80; server_name other.example.com; }\n'


def material(domain, extra=()):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    names = [domain, *extra]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=5))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
    )


class Panel:
    def __init__(self):
        self.rows, self.created = {}, []

    def inspect(self, domain, path):
        return self.rows.get((domain, path))

    def create_static(self, identity):
        self.created.append(identity['domain'])
        self.rows[(identity['domain'], identity['path'])] = dict(identity, panel_id=22)
        return 22


class Issuer:
    def __init__(self, mode='ok'):
        self.mode, self.domains, self.webroots = mode, [], []

    def issue(self, domain, webroot, destination):
        self.domains.append(domain)
        self.webroots.append(webroot)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.mode == 'fail':
            raise ProvisioningError(CERT_FAILED)
        fullchain, key = material('other.example.com' if self.mode == 'wrong' else domain,
                                  ('extra.example.com',) if self.mode == 'extra' else ())
        (destination / 'fullchain.pem').write_text(fullchain, encoding='utf-8')
        (destination / 'privkey.pem').write_text(key, encoding='utf-8')


class DNS:
    def __init__(self, ok=True):
        self.ok = ok

    def check(self, domain, expected):
        if not self.ok:
            raise ProvisioningError('A 记录尚未全部指向配置的服务器公网 IP；请移除冲突的 A 记录')
        return [expected]


def world(tmp_path, *, dns=True, issuer='ok', fail_tls=False, advance=True):
    registry = Registry(tmp_path / 'data')
    site = registry.add(DOMAIN)
    if advance:
        registry.transition(site['id'], site['generation'], 'dns_verified', 'ready')
    configs = tmp_path / 'vhosts'
    configs.mkdir()
    config = configs / (DOMAIN + '.conf')
    config.write_text('server { listen 80; server_name new.example.com; }\n', encoding='utf-8')
    other = configs / 'other.example.com.conf'
    other.write_text(OTHER, encoding='utf-8')
    calls = []

    def runner(args, **kwargs):
        calls.append(list(args))
        failed = fail_tls and args[-1:] == ['-t'] and sum(item[-1:] == ['-t'] for item in calls) >= 3
        return subprocess.CompletedProcess(args, 1 if failed else 0, '', '')

    setup = OriginSetup(
        registry, tmp_path / 'state', '8.8.8.8', Panel(), Issuer(issuer),
        certificate_root=tmp_path / 'certs', config_dir=configs, runner=runner, checker=DNS(dns))
    return setup, registry, site, config, other, calls


def test_matching_direct_domain_gets_one_site_one_certificate_and_https(tmp_path):
    setup, registry, site, config, other, calls = world(tmp_path)
    before = other.read_bytes()
    assert setup.run_pending() == 'done'
    saved = registry.get(site['id'])
    text = config.read_text(encoding='utf-8')
    assert saved['stage'] == 'active'
    assert saved['error'] == READY
    assert saved['panel_id'] == 22
    assert saved['managed_path'] == '/www/wwwroot/ab-lab-sites/' + site['id']
    assert setup.panel.created == [DOMAIN]
    assert setup.issuer.domains == [DOMAIN]
    assert setup.issuer.webroots == ['/www/wwwroot/ab-lab-sites/' + site['id']]
    assert 'proxy_pass http://127.0.0.1:8766;' in text
    assert 'proxy_set_header Host new.example.com;' in text
    assert 'listen 443 ssl;' in text
    assert text.count('server_name new.example.com;') == 2
    assert 'other.example.com' not in text
    assert other.read_bytes() == before
    assert calls[-1][-1] == 'reload'
    assert setup.run_pending() == 'done'
    assert setup.panel.created == [DOMAIN]
    assert registry.get(site['id'])['stage'] == 'active'


def test_nginx_test_failure_restores_only_this_site(tmp_path):
    setup, registry, site, config, other, calls = world(tmp_path, fail_tls=True)
    before = other.read_bytes()
    setup.run_pending()
    saved = registry.get(site['id'])
    text = config.read_text(encoding='utf-8')
    assert saved['stage'] == 'failed'
    assert saved['error'] == NGINX_FAILED
    assert 'listen 443' not in text
    assert 'proxy_pass http://127.0.0.1:8766;' in text
    assert other.read_bytes() == before
    assert calls[-1][-1] == '-t'
    assert setup.issuer.domains == [DOMAIN]


def test_certificate_failure_does_not_mark_the_site_active(tmp_path):
    setup, registry, site, config, other, _calls = world(tmp_path, issuer='fail')
    before = other.read_bytes()
    setup.run_pending()
    saved = registry.get(site['id'])
    assert saved['stage'] == 'failed'
    assert saved['error'] == CERT_FAILED
    assert 'listen 443' not in config.read_text(encoding='utf-8')
    assert other.read_bytes() == before
    assert setup.panel.created == [DOMAIN]


@pytest.mark.parametrize('mode', ['wrong', 'extra'])
def test_certificate_for_any_other_name_is_discarded(tmp_path, mode):
    setup, registry, site, config, other, _calls = world(tmp_path, issuer=mode)
    before = other.read_bytes()
    setup.run_pending()
    saved = registry.get(site['id'])
    assert saved['stage'] == 'failed'
    assert '不一致' in saved['error']
    assert 'listen 443' not in config.read_text(encoding='utf-8')
    assert other.read_bytes() == before
    assert not (setup.certificate_root / site['id'] / 'fullchain.pem').exists()


def test_origin_pass_builds_a_direct_domain_without_the_dns_service(tmp_path):
    setup, registry, site, config, other, _calls = world(tmp_path, advance=False)
    before = other.read_bytes()
    assert registry.get(site['id'])['stage'] == 'unconfigured'
    assert setup.run_pending() == 'done'
    assert registry.get(site['id'])['stage'] == 'active'
    assert setup.panel.created == [DOMAIN]
    assert 'listen 443 ssl;' in config.read_text(encoding='utf-8')
    assert other.read_bytes() == before


def test_origin_pass_leaves_cloudflare_domains_untouched(tmp_path):
    setup, registry, site, config, _other, _calls = world(tmp_path, advance=False)
    registry.set_cloudflare(site['id'], 'c' * 32, 'ada.ns.cloudflare.com', 'pending', '等待 NS')
    before = config.read_text(encoding='utf-8')
    setup.run_pending()
    saved = registry.get(site['id'])
    assert saved['stage'] == 'unconfigured'
    assert saved['cf_status'] == 'pending'
    assert saved['cf_detail'] == '等待 NS'
    assert setup.panel.created == []
    assert config.read_text(encoding='utf-8') == before


def test_cloudflare_and_unready_dns_do_not_create_a_site(tmp_path):
    setup, registry, site, config, _other, calls = world(tmp_path, dns=False)
    setup.run_pending()
    assert registry.get(site['id'])['stage'] == 'waiting_dns'
    assert setup.panel.created == []
    assert setup.issuer.domains == []
    assert calls == []
    registry.set_cloudflare(site['id'], 'c' * 32, 'ada.ns.cloudflare.com', 'pending', '等待 NS')
    current = registry.get(site['id'])
    registry.transition(site['id'], current['generation'], 'dns_verified', 'cloud')
    setup.checker.ok = True
    setup.run_pending()
    assert setup.panel.created == []
    assert setup.issuer.domains == []
    assert config.read_text(encoding='utf-8').startswith('server { listen 80;')


def test_retry_after_certificate_failure_does_not_create_another_site(tmp_path):
    setup, registry, site, _config, _other, _calls = world(tmp_path, issuer='fail')
    setup.run_pending()
    assert setup.panel.created == [DOMAIN]
    setup.issuer.mode = 'ok'
    registry.control(site['id'], 'retry')
    current = registry.get(site['id'])
    assert registry.transition(site['id'], current['generation'], 'dns_verified', 'again')
    setup.run_pending()
    assert setup.panel.created == [DOMAIN]
    assert setup.issuer.domains == [DOMAIN, DOMAIN]
    assert registry.get(site['id'])['stage'] == 'active'


def test_panel_port_stays_on_loopback(tmp_path):
    path = tmp_path / 'port.pl'
    path.write_text('8888\n', encoding='utf-8')
    assert panel_loopback_address(path) == 'http://127.0.0.1:8888'
    path.write_text('0\n', encoding='utf-8')
    with pytest.raises(ProvisioningError):
        panel_loopback_address(path)


def test_acme_worker_issues_one_name_without_deploying_other_sites(tmp_path):
    source = acme_worker_source()
    assert 'SetSSL' not in source and 'apply_cert_api' not in source
    assert 'client.sub_all_cert = refuse_other_sites' in source
    package = tmp_path / 'fake'
    package.mkdir()
    (package / 'acme_v2.py').write_text('''
import json, os
from pathlib import Path
calls = []
class acme_v2:
    def apply_cert(self, domains, auth_type, auth_to):
        calls.append({"domains": list(domains), "auth_type": auth_type, "auth_to": auth_to})
        cert = {"cert": "CERT\\n", "root": "ROOT\\n", "private_key": "KEY\\n", "domains": list(domains)}
        self.save_cert(cert, "1")
        cert["status"] = True
        Path(os.environ["AB_ORIGIN_CALLS"]).write_text(json.dumps(calls))
        return cert
    def save_cert(self, cert, index):
        calls.append("original-save")
        raise RuntimeError("original save_cert")
    def sub_all_cert(self, *args):
        calls.append("original-sub-all")
        raise RuntimeError("original sub_all_cert")
''', encoding='utf-8')
    site_id = 'a' * 32
    dest = tmp_path / 'out' / site_id
    calls = tmp_path / 'calls.json'
    env = os.environ.copy()
    env.update({
        'AB_ORIGIN_DOMAIN': DOMAIN,
        'AB_ORIGIN_WEBROOT': '/www/wwwroot/ab-lab-sites/' + site_id,
        'AB_ORIGIN_DEST': str(dest),
        'AB_ORIGIN_PYTHONPATH': str(package),
        'AB_ORIGIN_CALLS': str(calls),
    })
    result = subprocess.run([sys.executable, '-'], input=source, text=True, capture_output=True, env=env, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'ok\n'
    assert 'KEY' not in result.stdout and 'PRIVATE' not in result.stdout.upper()
    assert json.loads(calls.read_text(encoding='utf-8')) == [{
        'domains': [DOMAIN], 'auth_type': 'http', 'auth_to': '/www/wwwroot/ab-lab-sites/' + site_id,
    }]
    assert (dest / 'fullchain.pem').read_text(encoding='utf-8') == 'CERT\nROOT\n'
    assert (dest / 'privkey.pem').read_text(encoding='utf-8') == 'KEY\n'
