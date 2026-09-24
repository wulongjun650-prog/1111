"""Strict root-only assembly for the opt-in provisioning worker."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .certificate_acceptance import CertificateAcceptance, TlsRouteProbe
from .certificate_deployment import CertificateDeployment
from .certificate_versions import CertificateVersions, _private_directory
from .certbot_command import CertbotCommand
from .certificates import CertificateVerifier, _read_archive
from .managed_panel import ManagedPanel
from .nginx_entry import no_symlinks
from .nginx_tls import TlsEntry
from .panel_sites import PanelSites
from .provisioning import ProvisioningError, public_ipv4
from .provisioning_jobs import ProvisioningWorker
from .renewal_rehearsal import RenewalRehearsal
from .sites import Registry


CERTIFICATE_ROOT = Path('/var/lib/ab-lab-certificates')
WORKER_ROOT = Path('/var/lib/ab-lab-provision')
FIELDS = {
    'schema', 'enabled', 'server_ip', 'data_dir', 'registry_dir',
    'panel_address', 'panel_config', 'production_account_id',
    'staging_account_id', 'production_roots', 'staging_roots',
}
_effective_uid = getattr(os, 'geteuid', lambda: 0)


@dataclass(frozen=True)
class WorkerConfig:
    enabled: bool
    server_ip: str
    data_dir: Path
    registry_dir: Path
    panel_address: str
    panel_config: Path
    production_account_id: str
    staging_account_id: str
    production_roots: Path
    staging_roots: Path


def _read_private(path, limit):
    return _read_archive(Path(path), limit, private=True)


def _absolute(value):
    if not isinstance(value, str):
        raise ValueError('私有配置路径无效')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('私有配置路径必须是无父目录跳转的绝对路径')
    return path


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('私有配置不能包含重复字段')
        value[key] = item
    return value


def _state_directory(path):
    path = no_symlinks(Path(path))
    path.mkdir(mode=0o700, exist_ok=True)
    _private_directory(path)
    return path


def load_config(path):
    """Read one exact, private JSON document; disabled is a hard failure."""
    try:
        raw = _read_private(_absolute(str(path)), 128 * 1024)
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise ValueError('特权 worker 配置不可读取、权限不安全或格式无效') from None
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('特权 worker 配置字段不完整或包含未知字段')
    if type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('特权 worker 配置版本无效')
    if type(value['enabled']) is not bool or value['enabled'] is not True:
        raise ValueError('特权 worker 必须在私有配置中显式启用')

    try:
        url = urlsplit(value['panel_address'])
        port = url.port
    except (TypeError, ValueError):
        raise ValueError('面板地址无效') from None
    if (url.scheme not in ('http', 'https') or url.hostname != '127.0.0.1'
            or url.username or url.password or url.path or url.query or url.fragment
            or port is not None and not 1 <= port <= 65535):
        raise ValueError('面板地址必须是 127.0.0.1 根地址')

    account = re.compile(r'[a-f0-9]{32}')
    production = value['production_account_id']
    staging = value['staging_account_id']
    if (not isinstance(production, str) or account.fullmatch(production) is None
            or not isinstance(staging, str) or account.fullmatch(staging) is None
            or production == staging):
        raise ValueError('生产与 staging 必须使用不同的已登记账户')

    paths = {name: _absolute(value[name]) for name in (
        'data_dir', 'registry_dir', 'panel_config', 'production_roots', 'staging_roots')}
    if paths['production_roots'] == paths['staging_roots']:
        raise ValueError('生产与 staging 信任根必须分离')
    if not isinstance(value['server_ip'], str):
        raise ValueError('服务器公网 IPv4 必须是字符串')
    try:
        server_ip = public_ipv4(value['server_ip'])
    except (TypeError, ValueError):
        raise ValueError('服务器公网 IPv4 无效') from None
    return WorkerConfig(
        enabled=True, server_ip=server_ip, panel_address=value['panel_address'],
        production_account_id=production, staging_account_id=staging, **paths)


def assemble(settings):
    """Compose the complete privileged graph without starting a loop."""
    if not isinstance(settings, WorkerConfig) or settings.enabled is not True:
        raise ValueError('特权 worker 配置未显式启用')
    try:
        production_roots = _read_private(settings.production_roots, 2 * 1024 * 1024)
        staging_roots = _read_private(settings.staging_roots, 2 * 1024 * 1024)
    except (OSError, ValueError):
        raise ProvisioningError('证书信任根不可读取或权限不安全') from None

    worker_root = _state_directory(WORKER_ROOT)
    renewal_root = _state_directory(worker_root / 'renewal')
    panel_api = PanelSites.from_local_config(
        settings.panel_address, settings.panel_config, writes_enabled=True)
    panel = ManagedPanel(panel_api, worker_root / 'nginx')
    verifier = CertificateVerifier(production_roots, staging_roots)
    versions = CertificateVersions(verifier, CERTIFICATE_ROOT, writes_enabled=True)
    tls = TlsEntry(panel.nginx, versions)
    production = CertbotCommand('production', settings.production_account_id)
    staging = CertbotCommand('staging', settings.staging_account_id)
    deployment = CertificateDeployment(production, versions, tls, writes_enabled=True)
    renewal = RenewalRehearsal(staging, renewal_root, writes_enabled=True)
    acceptance = CertificateAcceptance(
        versions, TlsRouteProbe(settings.server_ip), renewal=renewal)
    panel.attach_certificates(deployment, acceptance)
    registry = Registry(settings.data_dir, registry_dir=settings.registry_dir)
    return ProvisioningWorker(
        registry, worker_root / 'jobs', settings.server_ip, panel)


def load_worker(path):
    if _effective_uid() != 0:
        raise PermissionError('特权 worker 必须以 root 运行')
    return assemble(load_config(path))


def run_pending(worker):
    """Run one bounded snapshot; the outer service owns scheduling."""
    count = 0
    for site in worker.registry.list():
        if site['id'] == 'default':
            continue
        worker.run_once(site['id'])
        count += 1
    return count
