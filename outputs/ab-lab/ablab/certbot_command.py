"""Internal Certbot command boundary, NOT a complete certificate manager.

No live callers: private directory/config auditing, account registration,
certificate verification and deployment must precede production integration.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

from .nginx_entry import no_symlinks
from .panel_sites import validate_identity
from .provisioning import ProvisioningError


SYSTEM_CLI = Path('/etc/letsencrypt/cli.ini')


def check_default_configs(root):
    """Reject ambient CLI files, not just hooks directories; never remove them.

    Linux Certbot default locations must be verified at deployment against the
    installed package. This check assumes root-owned, non-concurrently-mutated
    configuration ancestors; it is not a sandbox against a root administrator.
    """
    for path in (SYSTEM_CLI, Path(root) / '.config' / 'letsencrypt' / 'cli.ini'):
        try:
            no_symlinks(path).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ProvisioningError('无法核对 Certbot 默认配置，停止执行') from None
        raise ProvisioningError('存在 Certbot 默认配置，无法保证独立运行；未修改现有配置')


@dataclass(frozen=True)
class CertbotCommand:
    environment: str
    account_id: str

    def __post_init__(self):
        if self.environment not in ('production', 'staging'):
            raise ValueError('证书环境无效')
        if not isinstance(self.account_id, str) or not re.fullmatch('[a-f0-9]{32}', self.account_id):
            raise ValueError('必须提供已登记的证书账户 ID')

    def _common(self, identity):
        validate_identity(identity)
        root = '/var/lib/ab-lab-certificates/' + self.environment
        server = ('https://acme-v02.api.letsencrypt.org/directory'
                  if self.environment == 'production'
                  else 'https://acme-staging-v02.api.letsencrypt.org/directory')
        return (
            '--config', root + '/cli.ini', '--config-dir', root + '/config',
            '--work-dir', root + '/work', '--logs-dir', root + '/logs',
            '--server', server, '--account', self.account_id,
            '--cert-name', 'ab-' + identity['site_id'],
            '--non-interactive', '--no-directory-hooks',
        )

    def issue(self, identity):
        return ('/usr/bin/certbot', 'certonly', *self._common(identity),
                '--webroot', '--webroot-path', identity['path'], '-d', identity['domain'])

    def renew(self, identity, dry_run=False):
        if type(dry_run) is not bool or (dry_run and self.environment != 'staging'):
            raise ValueError('续期演练必须使用独立 staging 环境和账户')
        return ('/usr/bin/certbot', 'renew', *self._common(identity), '--no-random-sleep-on-renew',
                *(('--dry-run',) if dry_run else ()))

    def run(self, identity, *, operation, enabled=False, authorize, runner=subprocess.run):
        """Caller holds the worker lock, persisted intent and audited configs.

        authorize must recheck ownership and pause immediately before launch.
        None means exit 0 only; inspect and validate files separately. Unknown
        outcomes must be reconciled, never retried automatically by the caller.
        """
        if enabled is not True:
            raise ProvisioningError('独立证书执行未启用')
        if operation == 'issue':
            args = self.issue(identity)
        elif operation in ('renew', 'dry-run'):
            args = self.renew(identity, dry_run=operation == 'dry-run')
        else:
            raise ValueError('证书操作无效')
        root = '/var/lib/ab-lab-certificates/' + self.environment
        check_default_configs(root)
        if authorize() is not True:
            raise ProvisioningError('站点归属或暂停复核未通过，未执行证书操作')
        try:
            result = runner(args, shell=False, timeout=180, check=False,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=root,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': root,
                                 'XDG_CONFIG_HOME': root + '/.config'})
            if result.returncode != 0:
                raise ValueError('nonzero exit')
        except (OSError, subprocess.SubprocessError, ValueError):
            raise ProvisioningError('证书命令未确认成功；保留现场并核对结果，禁止盲目重试') from None
