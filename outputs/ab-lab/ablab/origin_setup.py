"""Build one direct-DNS site, then its certificate, before Cloudflare.

The root process reads the local panel API. It creates one static site, points
only that vhost at this application, and asks for a certificate for that one
domain. It does not deploy the certificate onto other sites. Orange-cloud
domains are left untouched because HTTP validation cannot see the origin.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key
from cryptography.x509.oid import NameOID

from .nginx_entry import NginxEntry, no_symlinks, read_bounded
from .panel_sites import validate_identity
from .provisioning import DNSChecker, ProvisioningError, public_ipv4
from .provisioning_jobs import worker_lock
from .sites import normalize_domain


ORIGIN_ROOT = Path('/var/lib/ab-lab-origin')
CERTIFICATE_ROOT = Path('/var/lib/ab-lab-certificates')
PANEL_PYTHON = '/www/server/panel/pyenv/bin/python'
PANEL_PORT = Path('/www/server/panel/data/port.pl')
READY = '本机 HTTPS 已接入。打开网站确认后，可以套用 Cloudflare。'
CERT_FAILED = '这个域名的证书没有申请成功。请确认 A 记录已经指向本机，稍后再试。'
NGINX_FAILED = 'Nginx 检查未通过，这个域名的配置已恢复。'
FOREIGN_SITE = '这个域名已在宝塔里，但不是本次创建的站点，已停止。'
RESUME_STAGES = {'dns_verified', 'creating', 'proxy', 'certificate'}


def panel_loopback_address(path=PANEL_PORT):
    try:
        port = Path(path).read_text(encoding='utf-8').strip()
    except OSError:
        raise ProvisioningError('读不到宝塔面板端口，自动建站已停止') from None
    if not re.fullmatch(r'[0-9]{2,5}', port):
        raise ProvisioningError('宝塔面板端口无效，自动建站已停止')
    number = int(port)
    if not 1 <= number <= 65535:
        raise ProvisioningError('宝塔面板端口无效，自动建站已停止')
    return 'http://127.0.0.1:' + str(number)


def acme_worker_source():
    """Panel interpreter source. save_cert is replaced before the order runs."""
    return r'''
import os, re, sys, tempfile
domain = os.environ.get('AB_ORIGIN_DOMAIN', '')
webroot = os.environ.get('AB_ORIGIN_WEBROOT', '')
dest = os.environ.get('AB_ORIGIN_DEST', '')
site_id = webroot.rsplit('/', 1)[-1]
temp_root = tempfile.gettempdir().rstrip('/') + '/'
if (not re.fullmatch(r'[a-z0-9.-]{1,253}', domain)
        or not re.fullmatch(r'/www/wwwroot/ab-lab-sites/[a-f0-9]{32}', webroot)
        or site_id != dest.rsplit('/', 1)[-1]
        or '..' in dest
        or not dest.startswith(('/var/lib/ab-lab-certificates/', temp_root))):
    sys.exit(2)
sys.path.insert(0, '/www/server/panel/class')
extra = os.environ.get('AB_ORIGIN_PYTHONPATH', '')
if extra:
    sys.path.insert(0, extra)
import acme_v2
client = acme_v2.acme_v2()

def isolated_save(cert, index):
    if cert.get('domains') != [domain]:
        raise RuntimeError('certificate names do not match this domain')
    os.makedirs(dest, 0o700, exist_ok=True)
    fullchain = cert['cert'] + cert.get('root', '')
    for name, text, mode in (('fullchain.pem', fullchain, 0o644), ('privkey.pem', cert['private_key'], 0o600)):
        target = os.path.join(dest, name)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(text)

def refuse_other_sites(*_args, **_kwargs):
    raise RuntimeError('refusing to update any other certificate')

client.save_cert = isolated_save
client.sub_all_cert = refuse_other_sites
result = client.apply_cert([domain], 'http', webroot)
if not isinstance(result, dict) or result.get('status') is not True or result.get('domains') != [domain]:
    sys.exit(1)
if not os.path.isfile(os.path.join(dest, 'fullchain.pem')) or not os.path.isfile(os.path.join(dest, 'privkey.pem')):
    sys.exit(1)
sys.stdout.write('ok\n')
'''


def _names(certificate):
    names = set()
    try:
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names.update(extension.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        pass
    for attribute in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        if isinstance(attribute.value, str):
            names.add(attribute.value)
    return names


def validate_material(domain, fullchain_pem, key_pem):
    domain = normalize_domain(domain)
    try:
        certificate = x509.load_pem_x509_certificate(fullchain_pem.encode())
        key = load_pem_private_key(key_pem.encode(), password=None)
        start = certificate.not_valid_before_utc
        end = certificate.not_valid_after_utc
        leaf = certificate.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        owned = key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        names = _names(certificate)
    except (ValueError, TypeError, AttributeError):
        raise ProvisioningError('证书文件无法读取，未写入 Nginx。') from None
    now = datetime.now(timezone.utc)
    if names != {domain} or leaf != owned or start > now or end <= now:
        raise ProvisioningError('证书名称与这个域名不一致，未写入 Nginx。')


def _safe_pem(path, site_id, name):
    text = no_symlinks(path).as_posix()
    if not re.fullmatch(r'[A-Za-z0-9_./:-]+', text) or not text.endswith('/' + site_id + '/' + name):
        raise ProvisioningError('证书路径无效，未写入 Nginx。')
    return text


def render_origin_https(identity, fullchain, key_path):
    validate_identity(identity)
    fullchain = _safe_pem(fullchain, identity['site_id'], 'fullchain.pem')
    key_path = _safe_pem(key_path, identity['site_id'], 'privkey.pem')
    return f'''# AB Lab managed HTTPS entry {identity['site_id']}
server {{
    listen 80;
    server_name {identity['domain']};
    root {identity['path']};
    location ^~ /.well-known/acme-challenge/ {{
        default_type text/plain;
        try_files $uri =404;
    }}
    location / {{ return 308 https://{identity['domain']}$request_uri; }}
}}
server {{
    listen 443 ssl;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_certificate "{fullchain}";
    ssl_certificate_key "{key_path}";
    server_name {identity['domain']};
    root {identity['path']};
    client_max_body_size 20m;
    location ^~ /.well-known/acme-challenge/ {{
        default_type text/plain;
        try_files $uri =404;
    }}
    location ^~ / {{
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host {identity['domain']};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For "";
        proxy_set_header Forwarded "";
        proxy_cache off;
        proxy_buffering off;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
        proxy_intercept_errors off;
    }}
}}
'''


class PanelCertificateIssuer:
    """One domain, HTTP-01, files kept in our directory. No panel-wide deploy."""

    def __init__(self, python=PANEL_PYTHON, runner=subprocess.run):
        self.python, self.runner = python, runner

    def issue(self, domain, webroot, destination):
        normalize_domain(domain)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = os.environ.copy()
        env.update(AB_ORIGIN_DOMAIN=domain, AB_ORIGIN_WEBROOT=webroot, AB_ORIGIN_DEST=str(destination))
        try:
            result = self.runner(
                [self.python, '-'], input=acme_worker_source(), text=True,
                capture_output=True, timeout=180, env=env, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ProvisioningError(CERT_FAILED) from None
        if result.returncode != 0:
            raise ProvisioningError(CERT_FAILED)


class OriginSetup:
    def __init__(self, registry, state_dir, server_ip, panel, issuer, *,
                 certificate_root=CERTIFICATE_ROOT, config_dir=Path('/www/server/panel/vhost/nginx'),
                 runner=subprocess.run, checker=None, cloudflare=None):
        self.registry, self.panel, self.issuer = registry, panel, issuer
        self.cloudflare = cloudflare
        self.server_ip = public_ipv4(server_ip)
        self.root = no_symlinks(Path(state_dir))
        self.certificate_root = no_symlinks(Path(certificate_root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.job_dir = self.root / 'jobs'
        self.job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.certificate_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = self.root / 'worker.lock'
        self.checker = checker
        self.nginx = NginxEntry(
            self.root / 'nginx', panel.inspect, config_dir=config_dir,
            runner=runner, writes_enabled=True)

    def run_pending(self):
        with worker_lock(self.lock_path) as acquired:
            if not acquired:
                return 'busy'
            from .cloudflare import repair_strict_ssl
            from .provisioning import inspect_pending
            repair_strict_ssl(self.registry, self.cloudflare)
            inspect_pending(self.registry, self.server_ip, self.checker, skip_cloudflare=True)
            for site in self.registry.list():
                if self._eligible(site):
                    self._run(site)
            return 'done'

    def _eligible(self, site):
        return (site['id'] != 'default' and site['enabled'] and not site.get('cf_zone_id')
                and not site.get('cf_status') and site['stage'] in RESUME_STAGES
                and site['next_attempt'] <= time.time())

    def _current(self, site):
        latest = self.registry.get(site['id'])
        return latest['enabled'] and latest['generation'] == site['generation'] and latest['domain'] == site['domain']

    def _stage(self, site, stage, **kwargs):
        return self._current(site) and self.registry.transition(site['id'], site['generation'], stage, **kwargs)

    def _run(self, site):
        checker = self.checker or DNSChecker()
        try:
            checker.check(site['domain'], self.server_ip)
        except ProvisioningError as error:
            self._stage(site, 'waiting_dns', detail=str(error)[:180], delay=300)
            return
        if not self._current(site):
            return
        try:
            self._process(site)
        except ProvisioningError as error:
            self._stage(site, 'failed', detail=str(error)[:180], delay=3600)
        except Exception:
            self._stage(site, 'failed', detail='建站未完成。这个域名已停止，稍后再试。', delay=3600)

    def _job_path(self, site_id):
        return no_symlinks(self.job_dir / (site_id + '.json'))

    def _read_job(self, site):
        path = self.job_dir / (site['id'] + '.json')
        if not path.exists():
            job = {
                'site_id': site['id'], 'domain': site['domain'],
                'path': '/www/wwwroot/ab-lab-sites/' + site['id'],
                'owner': uuid.uuid4().hex, 'phase': 'new', 'panel_id': None,
            }
            self._write_job(job)
            return job
        try:
            job = json.loads(read_bounded(path))
            identity = {key: job[key] for key in ('site_id', 'domain', 'path', 'owner')}
            validate_identity(identity)
            if (identity['site_id'] != site['id'] or identity['domain'] != site['domain']
                    or job['phase'] not in ('new', 'site', 'http', 'certificate', 'ready')
                    or (job['panel_id'] is not None and (type(job['panel_id']) is not int or job['panel_id'] <= 0))):
                raise ValueError('job mismatch')
        except (ProvisioningError, ValueError, KeyError, TypeError):
            raise ProvisioningError('建站记录不一致，已停止，需要人工核对。') from None
        return job

    def _write_job(self, job):
        path = self._job_path(job['site_id'])
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _identity(self, job):
        return {key: job[key] for key in ('site_id', 'domain', 'path', 'owner')}

    def _process(self, site):
        job = self._read_job(site)
        identity = self._identity(job)
        if job['phase'] in ('new', 'site'):
            if not self._stage(site, 'creating', managed_path=identity['path']):
                return
            job['panel_id'] = self._ensure_site(identity, job['panel_id'])
            job['phase'] = 'site'
            self._write_job(job)
        if job['phase'] == 'site':
            if not self._stage(site, 'proxy', panel_id=job['panel_id'], managed_path=identity['path']):
                return
            self._ensure_http(identity, job['panel_id'])
            job['phase'] = 'http'
            self._write_job(job)
        if job['phase'] == 'http':
            if not self._stage(site, 'certificate', panel_id=job['panel_id'], managed_path=identity['path']):
                return
            self._ensure_cert(identity)
            job['phase'] = 'certificate'
            self._write_job(job)
        if job['phase'] == 'certificate':
            if not self._cloudflare_https_ready(site):
                return
            fullchain, key_path = self._cert_paths(identity)
            self._publish(identity, render_origin_https(identity, fullchain, key_path))
            job['phase'] = 'ready'
            self._write_job(job)
        if job['phase'] == 'ready':
            self._stage(site, 'active', detail=READY, panel_id=job['panel_id'], managed_path=identity['path'])

    def _ensure_site(self, identity, panel_id):
        remote = self.panel.inspect(identity['domain'], identity['path'])
        if remote is None:
            created = self.panel.create_static(identity)
            remote = self.panel.inspect(identity['domain'], identity['path'])
            if remote is None or remote.get('panel_id') != created:
                raise ProvisioningError('创建结果不明确，未继续申请证书。')
        elif (remote.get('site_id') != identity['site_id'] or remote.get('owner') != identity['owner']
              or remote.get('path') != identity['path'] or remote.get('domain') != identity['domain']):
            raise ProvisioningError(FOREIGN_SITE)
        if panel_id is not None and remote['panel_id'] != panel_id:
            raise ProvisioningError(FOREIGN_SITE)
        if type(remote.get('panel_id')) is not int or remote['panel_id'] <= 0:
            raise ProvisioningError('创建结果不明确，未继续申请证书。')
        return remote['panel_id']

    def _ensure_http(self, identity, panel_id):
        _config, receipt = self.nginx._paths(identity)
        if not receipt.exists():
            self.nginx.capture_created(identity, panel_id)
        self.nginx.configure(identity, panel_id)

    def _cert_paths(self, identity):
        directory = no_symlinks(self.certificate_root / identity['site_id'])
        return directory / 'fullchain.pem', directory / 'privkey.pem'

    def _ensure_cert(self, identity):
        fullchain, key_path = self._cert_paths(identity)
        if fullchain.exists() and key_path.exists():
            try:
                validate_material(identity['domain'], fullchain.read_text(encoding='utf-8'), key_path.read_text(encoding='utf-8'))
                return
            except ProvisioningError:
                fullchain.unlink(missing_ok=True)
                key_path.unlink(missing_ok=True)
        self.issuer.issue(identity['domain'], identity['path'], fullchain.parent)
        try:
            validate_material(identity['domain'], fullchain.read_text(encoding='utf-8'), key_path.read_text(encoding='utf-8'))
        except (ProvisioningError, OSError):
            fullchain.unlink(missing_ok=True)
            key_path.unlink(missing_ok=True)
            raise ProvisioningError('证书名称与这个域名不一致，未写入 Nginx。') from None

    def _cloudflare_https_ready(self, site):
        """Do not turn on the HTTP redirect while Cloudflare still uses plain HTTP."""
        latest = self.registry.get(site['id'])
        if not latest.get('cf_zone_id'):
            return True
        if self.cloudflare is None or not self.cloudflare.token:
            self._stage(site, 'certificate', detail='Cloudflare 加密回源还不能修改，先不开启强制跳转。', delay=60)
            return False
        try:
            self.cloudflare.ensure_strict_ssl(latest['cf_zone_id'], latest['domain'])
        except ProvisioningError:
            self._stage(site, 'certificate', detail='Cloudflare 还没改成加密回源，先不开启强制跳转。', delay=60)
            return False
        return True

    def _publish(self, identity, text):
        config, _receipt = self.nginx._paths(identity)
        config = no_symlinks(config)
        siblings = [item for item in config.parent.iterdir() if item.is_file() and item != config]
        before = {item: item.read_bytes() for item in siblings}
        original = read_bounded(config)
        config.write_text(text, encoding='utf-8', newline='\n')
        try:
            self.nginx._nginx('-t')
        except ProvisioningError:
            config.write_text(original, encoding='utf-8', newline='\n')
            self._same(before)
            raise ProvisioningError(NGINX_FAILED) from None
        self._same(before)
        try:
            self.nginx._nginx('-s', 'reload')
        except ProvisioningError:
            self._same(before)
            raise ProvisioningError(NGINX_FAILED) from None
        self._same(before)

    def _same(self, before):
        for path, content in before.items():
            if path.read_bytes() != content:
                raise ProvisioningError('其他站点配置发生变化，已停止。')


def serve_origin(data_dir, server_ip, *, port_path=PANEL_PORT, api_path=Path('/www/server/panel/config/api.json')):
    if os.geteuid() != 0:
        raise SystemExit('origin setup must run as root')
    from .panel_sites import PanelSites
    from .sites import Registry
    try:
        server_ip = public_ipv4(server_ip)
        from .cloudflare import Cloudflare
        panel = PanelSites.from_local_config(panel_loopback_address(port_path), api_path, writes_enabled=True)
        cloudflare = Cloudflare(os.environ.get('AB_CLOUDFLARE_TOKEN', ''), os.environ.get('AB_CLOUDFLARE_TEMPLATE', ''), server_ip)
        setup = OriginSetup(
            Registry(data_dir), ORIGIN_ROOT, server_ip, panel, PanelCertificateIssuer(),
            cloudflare=cloudflare if cloudflare.token else None)
    except (ProvisioningError, ValueError, OSError) as error:
        print(str(error), flush=True)
        raise SystemExit(1) from None
    print('Origin setup watches domains whose A record points at this server.', flush=True)
    while True:
        try:
            setup.run_pending()
        except Exception as error:
            print('Origin pass failed (' + type(error).__name__ + ').', flush=True)
        time.sleep(60)
