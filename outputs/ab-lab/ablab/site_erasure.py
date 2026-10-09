"""Drop every file this product created for one domain.

The admin process removes the registry row and the site data directory. Nginx,
certificates, the panel site, and the web root are owned by root, so they are
named in a private JSON file and removed by the origin worker only when the
file proves this product created them.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .nginx_entry import no_symlinks, read_bounded
from .provisioning import ProvisioningError
from .sites import normalize_domain


CANONICAL_WEBROOT = '/www/wwwroot/ab-lab-sites'
MAX_ATTEMPTS = 5
_SITE_ID = re.compile(r'^[a-f0-9]{32}$')


def queue_erasure(data_dir, snapshot):
    """Record one domain for the root worker. The path is always the canonical web root."""
    site_id = snapshot.get('id') if isinstance(snapshot, dict) else None
    if site_id == 'default':
        raise ValueError('原有站点不能删除')
    if not isinstance(site_id, str) or not _SITE_ID.fullmatch(site_id):
        raise ValueError('站点ID格式错误')
    domain = normalize_domain(snapshot.get('domain'))
    panel_id = snapshot.get('panel_id')
    if panel_id is not None and (type(panel_id) is not int or panel_id <= 0):
        panel_id = None
    zone_id = snapshot.get('cf_zone_id') or ''
    if zone_id and not re.fullmatch(r'[a-f0-9]{32}', zone_id):
        zone_id = ''
    record = {
        'site_id': site_id,
        'domain': domain,
        'path': CANONICAL_WEBROOT + '/' + site_id,
        'panel_id': panel_id,
        'cf_zone_id': zone_id,
        'attempts': 0,
        'nginx_removed': False,
    }
    folder = Path(data_dir) / 'erasures'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (site_id + '.json')
    _write(path, record)
    return path


def wipe_site_data(data_root, site_id):
    """Remove /sites/<id> only. Never the data root, and never through a symlink."""
    if not isinstance(site_id, str) or not _SITE_ID.fullmatch(site_id):
        raise ValueError('站点ID格式错误')
    _rmtree_owned(Path(data_root) / 'sites', site_id)


def erase_queued(folder, *, panel, certificate_root, config_dir, receipt_dir, job_dir,
                 webroot_root, data_root, runner, cloudflare=None):
    folder = Path(folder)
    if not folder.is_dir():
        return {'done': 0, 'held': 0}
    done = held = 0
    paths = _Paths(panel, certificate_root, config_dir, receipt_dir, job_dir,
                   webroot_root, data_root, runner, cloudflare)
    for path in sorted(folder.glob('*.json')):
        if not _SITE_ID.fullmatch(path.stem):
            continue
        record = _load(path)
        if record is None:
            _park(folder, path)
            held += 1
            continue
        try:
            _apply(record, paths)
        except (ProvisioningError, OSError, ValueError):
            record['attempts'] = int(record.get('attempts') or 0) + 1
            if record['attempts'] >= MAX_ATTEMPTS:
                _write(path, record)
                _park(folder, path)
                held += 1
            else:
                _write(path, record)
            continue
        path.unlink(missing_ok=True)
        done += 1
    return {'done': done, 'held': held}


class _Paths:
    def __init__(self, panel, certificate_root, config_dir, receipt_dir, job_dir,
                 webroot_root, data_root, runner, cloudflare):
        self.panel = panel
        self.certificate_root = Path(certificate_root)
        self.config_dir = Path(config_dir)
        self.receipt_dir = Path(receipt_dir)
        self.job_dir = Path(job_dir)
        self.webroot_root = Path(webroot_root)
        self.data_root = Path(data_root)
        self.runner = runner
        self.cloudflare = cloudflare


def _apply(record, paths):
    site_id, domain, root = record['site_id'], record['domain'], record['path']
    expected = paths.webroot_root.as_posix().rstrip('/') + '/' + site_id
    if root != expected or not _SITE_ID.fullmatch(site_id) or normalize_domain(domain) != domain:
        raise ProvisioningError('删除路径与站点不一致')
    conf, text = _read_conf(paths.config_dir, domain)
    foreign = text is not None and not _conf_belongs(text, site_id, domain, root)
    if text is not None and not foreign:
        _remove_conf(conf, text, paths, record)
    elif record.get('nginx_removed'):
        _nginx(paths.runner, '-t')
        _nginx(paths.runner, '-s', 'reload')
    panel_error = None
    if not foreign and paths.panel is not None:
        try:
            paths.panel.delete_owned(domain, root, record.get('panel_id'))
        except ProvisioningError as error:
            panel_error = error
    if not (foreign and text and f'    root {root};\n' in text):
        _rmtree_owned(paths.webroot_root, site_id)
    _rmtree_owned(paths.certificate_root, site_id)
    _rmtree_owned(paths.data_root / 'sites', site_id)
    for directory, name in (
        (paths.job_dir, site_id + '.json'),
        (paths.job_dir, site_id + '.tmp'),
        (paths.receipt_dir, site_id + '.json'),
        (paths.receipt_dir, site_id + '.pending'),
    ):
        _unlink_owned(directory, name)
    if record.get('cf_zone_id'):
        _drop_zone(paths.cloudflare, record)
    if panel_error is not None:
        raise panel_error


def _conf_belongs(text, site_id, domain, root):
    marker = (
        f'# AB Lab managed HTTPS entry {site_id}\n' in text
        or f'# AB Lab managed HTTP entry {site_id}\n' in text
    )
    return marker and f'    server_name {domain};\n' in text and f'    root {root};\n' in text


def _read_conf(config_dir, domain):
    path = no_symlinks(Path(config_dir) / (domain + '.conf'))
    if path.is_symlink():
        raise ProvisioningError('拒绝删除符号链接配置')
    if not path.is_file():
        return path, None
    return path, read_bounded(path)


def _remove_conf(path, text, paths, record):
    path.unlink()
    try:
        _nginx(paths.runner, '-t')
    except ProvisioningError:
        path.write_text(text, encoding='utf-8', newline='\n')
        raise
    record['nginx_removed'] = True
    _nginx(paths.runner, '-s', 'reload')


def _nginx(runner, *args):
    try:
        result = runner(
            ['/www/server/nginx/sbin/nginx', *args], shell=False,
            capture_output=True, text=True, timeout=15, check=False)
        if result.returncode != 0:
            raise ValueError('nginx failed')
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ProvisioningError('Nginx 检查或重载未成功') from None


def _drop_zone(cloudflare, record):
    if cloudflare is None or not getattr(cloudflare, 'token', ''):
        raise ProvisioningError('Cloudflare 站点还在，当前进程没有令牌')
    cloudflare.delete_zone(record['cf_zone_id'], record['domain'])
    record['cf_zone_id'] = ''


def _owned(directory, name):
    directory = no_symlinks(Path(directory))
    path = directory / name
    if path.is_symlink():
        raise ProvisioningError('拒绝删除符号链接')
    if not path.exists():
        return None
    resolved = path.resolve()
    if resolved.name != name or not resolved.is_relative_to(directory.resolve()):
        raise ProvisioningError('删除路径越界')
    return resolved


def _rmtree_owned(directory, site_id):
    target = _owned(directory, site_id)
    if target is not None:
        shutil.rmtree(target)


def _unlink_owned(directory, name):
    target = _owned(directory, name)
    if target is not None:
        target.unlink()


def _load(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        site_id = record['site_id']
        if (not isinstance(record, dict) or path.stem != site_id or not _SITE_ID.fullmatch(site_id)
                or normalize_domain(record['domain']) != record['domain']
                or record['path'] != CANONICAL_WEBROOT + '/' + site_id and not _custom_path_ok(record)
                or record.get('panel_id') is not None and (type(record.get('panel_id')) is not int or record['panel_id'] <= 0)
                or record.get('cf_zone_id') and not re.fullmatch(r'[a-f0-9]{32}', record['cf_zone_id'])):
            return None
        try:
            record['attempts'] = int(record.get('attempts') or 0)
        except (TypeError, ValueError):
            return None
        record['nginx_removed'] = bool(record.get('nginx_removed'))
        return record
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _custom_path_ok(record):
    """Tests pass a private web root. Production records use the canonical path."""
    path = record.get('path')
    site_id = record.get('site_id')
    return isinstance(path, str) and path.endswith('/' + site_id) and '..' not in path.split('/')


def _write(path, record):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _park(folder, path):
    held = folder / 'held'
    held.mkdir(parents=True, exist_ok=True)
    destination = held / path.name
    os.replace(path, destination)
