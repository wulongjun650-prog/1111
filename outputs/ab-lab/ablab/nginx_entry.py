"""Transactional HTTP entry for newly created, owned aaPanel sites only.

Not wired into a live worker. TLS issuance/deployment is a separate unfinished
step. Call capture_created only after a successful owned AddSite transaction.
All calls must be serialized by the provisioning worker lock.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .panel_sites import validate_identity
from .provisioning import ProvisioningError


def no_symlinks(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ProvisioningError('受管路径不能含父目录跳转')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ProvisioningError('受管路径含符号链接，停止写入')
    return path


def read_bounded(path, limit=256 * 1024):
    no_symlinks(path)
    try:
        with path.open('rb') as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError('oversized')
        return data.decode('utf-8')
    except (OSError, UnicodeError, ValueError):
        raise ProvisioningError('受管配置或备份不可读取，停止写入') from None


def sync_directory(path):
    if os.name != 'nt':
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def exclusive_text(path, text, mode=0o600):
    """Publish a new private record; never replace an existing record."""
    no_symlinks(path)
    fd, name = tempfile.mkstemp(prefix='.ab-lab-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, mode)
        os.link(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def replace_preserving(staged, target):
    """Keep the exact displaced file. No lossy rename fallback is permitted.

    Linux: rename(2), RENAME_EXCHANGE. Windows development: ReplaceFileW with
    an explicit backup. Failure may be partial, so callers retain all files.
    """
    if sys.platform == 'linux':
        exchange = ctypes.CDLL(None, use_errno=True).renameat2
        exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        exchange.restype = ctypes.c_int
        if exchange(-100, os.fsencode(staged), -100, os.fsencode(target), 2) != 0:
            raise OSError(ctypes.get_errno(), 'exchange failed')
        return staged
    if os.name == 'nt':
        backup = staged.with_name('displaced')
        replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
        replace.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        replace.restype = ctypes.c_int
        if not replace(str(target), str(staged), str(backup), 0, None, None):
            raise OSError(ctypes.get_last_error(), 'replacement failed')
        return backup
    raise OSError('preserving replacement unsupported')


def guarded_config(path, text, expected, pending):
    """Preserve displaced data, stop on conflict, leave uncertain writes blocked.

    Retained snapshots live in private hidden subdirectories beside the config
    (same filesystem, outside the panel's *.conf include). A pending record
    survives a crash/error; retries require manual reconciliation, not reload.
    This does not lock out an administrator or undo their later writes.
    """
    no_symlinks(path)
    try:
        stage = Path(tempfile.mkdtemp(prefix='.ab-lab-', dir=path.parent))
        staged = stage / 'previous'
        exclusive_text(staged, text, 0o644)
        sync_directory(path.parent)
        exclusive_text(pending, json.dumps({'config': str(path), 'snapshot': str(stage)}))
        no_symlinks(path)
        displaced = replace_preserving(staged, path)
        sync_directory(stage)
        sync_directory(path.parent)
        if read_bounded(displaced) != expected or read_bounded(path) != text:
            raise ProvisioningError('配置发生并发变化；已保留现场备份，需人工核对，禁止重载和重试')
        pending.unlink()
        sync_directory(pending.parent)
    except (OSError, AttributeError):
        raise ProvisioningError('配置写入结果不确定或文件系统不支持；保留现场文件，需人工核对') from None


def render_http_entry(identity):
    validate_identity(identity)
    return f'''# AB Lab managed HTTP entry {identity['site_id']}
# HTTPS is not yet configured; application rejects plaintext public access.
server {{
    listen 80;
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


class NginxEntry:
    def __init__(self, state_dir, inspect, *, config_dir=Path('/www/server/panel/vhost/nginx'),
                 runner=subprocess.run, writes_enabled=False):
        self.root = no_symlinks(Path(state_dir))
        self.config_dir = no_symlinks(Path(config_dir))
        if self.root.is_relative_to(self.config_dir) or self.config_dir.is_relative_to(self.root):
            raise ValueError('配置备份必须在独立私有目录，不能与 Nginx 配置目录重叠')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            info = self.root.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('配置备份目录必须由服务账号独占，权限 0700')
        self.inspect, self.runner = inspect, runner
        self.writes_enabled = writes_enabled is True

    def _owned(self, identity, panel_id):
        validate_identity(identity)
        if not self.writes_enabled:
            raise ProvisioningError('Nginx 自动写入尚未启用')
        remote = self.inspect(identity['domain'], identity['path'])
        if (type(panel_id) is not int or panel_id <= 0
                or remote != dict(identity, panel_id=panel_id)):
            raise ProvisioningError('站点所有权已变化，停止配置')

    def _paths(self, identity):
        return (no_symlinks(self.config_dir / (identity['domain'] + '.conf')),
                no_symlinks(self.root / (identity['site_id'] + '.json')))

    def capture_created(self, identity, panel_id):
        self._owned(identity, panel_id)
        config, receipt = self._paths(identity)
        if receipt.exists():
            raise ProvisioningError('创建备份已存在，不能覆盖原始记录')
        baseline = read_bounded(config)
        if 'ssl_certificate' in baseline or 'proxy_pass' in baseline:
            raise ProvisioningError('新建静态站点已含证书或代理配置，需人工核对')
        record = dict(identity=identity, panel_id=panel_id, baseline=baseline,
                      desired=render_http_entry(identity))
        exclusive_text(receipt, json.dumps(record, ensure_ascii=False))

    def _nginx(self, *args):
        try:
            result = self.runner(['/www/server/nginx/sbin/nginx', *args], shell=False,
                                 capture_output=True, text=True, timeout=15, check=False)
            if result.returncode != 0:
                raise ValueError('nginx failed')
        except (OSError, subprocess.SubprocessError, ValueError):
            raise ProvisioningError('Nginx 检查或重载未成功；请在服务器核对，原始备份已保留') from None

    def configure(self, identity, panel_id):
        self._owned(identity, panel_id)
        config, receipt = self._paths(identity)
        pending = no_symlinks(receipt.with_suffix('.pending'))
        if pending.exists():
            raise ProvisioningError('上次配置写入未确认，保留现场备份，需人工核对后才能继续')
        try:
            # JSON escaping can grow each baseline byte up to six bytes.
            record = json.loads(read_bounded(receipt, limit=2 * 1024 * 1024))
            if (record['identity'] != identity or record['panel_id'] != panel_id
                    or record['desired'] != render_http_entry(identity)):
                raise ValueError('receipt mismatch')
            baseline, desired = record['baseline'], record['desired']
        except (ValueError, KeyError, TypeError):
            raise ProvisioningError('创建备份缺失或不一致，停止配置') from None
        current = read_bounded(config)
        if current not in (baseline, desired):
            root_line = f"    root {identity['path']};\n"
            stats_line = (f"    include {self.config_dir.as_posix()}/extension/"
                          f"{identity['domain']}/*.conf;\n")
            if baseline.count(root_line) != 1 or current != baseline.replace(
                    root_line, root_line + stats_line, 1):
                raise ProvisioningError('站点配置已被修改，保留人工修改并停止接入')
        self._nginx('-t')
        self._owned(identity, panel_id)
        if read_bounded(config) != current:
            raise ProvisioningError('检查期间配置发生变化，停止写入')
        if current != desired:
            guarded_config(config, desired, current, pending)
        try:
            self._nginx('-t')
        except ProvisioningError:
            # Do not clobber an operator edit or roll back unrelated sites.
            if current != desired and read_bounded(config) == desired:
                self._owned(identity, panel_id)
                guarded_config(config, current, desired, pending)
            raise
        self._owned(identity, panel_id)
        if read_bounded(config) != desired:
            raise ProvisioningError('重载前配置发生变化，停止重载')
        self._nginx('-s', 'reload')
