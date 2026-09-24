"""Local filesystem guard joining site creation and managed configuration."""
from pathlib import Path
import re
import subprocess

from .nginx_entry import NginxEntry, no_symlinks
from .panel_sites import validate_identity
from .provisioning import ProvisioningError, ProvisioningNotStarted


class ManagedPanel:
    def __init__(self, api, state_dir, *, entry_root=Path('/www/wwwroot/ab-lab-sites'),
                 config_dir=Path('/www/server/panel/vhost/nginx'), runner=subprocess.run,
                 certificate_deployment=None, acceptance=None):
        self.api = api
        self.entry_root = no_symlinks(Path(entry_root))
        private = no_symlinks(Path(state_dir))
        if private.is_relative_to(self.entry_root) or self.entry_root.is_relative_to(private):
            raise ValueError('私有备份不能与公开入口目录重叠')
        self.nginx = NginxEntry(state_dir, self.inspect, config_dir=config_dir,
                                runner=runner, writes_enabled=api.writes_enabled)
        self.certificate_deployment = certificate_deployment
        if acceptance is not None and (certificate_deployment is None
                or getattr(acceptance, 'versions', None) is not certificate_deployment.versions):
            raise ValueError('证书部署与验收必须使用同一个不可变版本存储')
        self.acceptance = acceptance

    def _directory(self, path):
        if not isinstance(path, str) or not re.fullmatch('/www/wwwroot/ab-lab-sites/[a-f0-9]{32}', path):
            raise ValueError('入口路径不在固定受管目录中')
        return no_symlinks(self.entry_root / path.rsplit('/', 1)[1])

    def inspect(self, domain, path):
        directory = self._directory(path)
        remote = self.api.inspect(domain, path)
        # API validates the hostname before it can become a config filename.
        config = no_symlinks(self.nginx.config_dir / (domain + '.conf'))
        if remote is None:
            if directory.exists() or config.exists():
                raise ProvisioningError('域名未登记在面板，但入口目录或配置已存在，拒绝覆盖')
        elif not directory.is_dir() or not config.is_file():
            raise ProvisioningError('面板记录与磁盘目录或配置不一致，停止接入')
        return remote

    def create(self, identity):
        validate_identity(identity)
        if not self.api.writes_enabled:
            raise ProvisioningError('面板自动写入未启用')
        if self.inspect(identity['domain'], identity['path']) is not None:
            raise ProvisioningError('站点已存在，不能重新创建')
        _, receipt = self.nginx._paths(identity)
        if receipt.exists():
            raise ProvisioningError('存在历史创建备份，需要人工核对，不重新创建')
        # Only the fixed dedicated parent is made locally; AddSite creates the
        # per-ID directory. An existing directory is never adopted or emptied.
        no_symlinks(self.entry_root)
        self.entry_root.mkdir(mode=0o755, exist_ok=True)
        panel_id = self.api.create_static(identity)
        # A timeout before this point cannot prove the first config belongs to
        # this creation, so recovery without a receipt fails conservatively.
        self.nginx.capture_created(identity, panel_id)

    def configure(self, identity, panel_id):
        self.nginx.configure(identity, panel_id)

    def _certificate_authorization(self, identity, panel_id, authorize):
        def owned_and_current():
            if authorize() is not True:
                return False
            try:
                remote = self.inspect(identity['domain'], identity['path'])
            except (ProvisioningError, KeyError, TypeError):
                return False
            return authorize() is True and remote == dict(identity, panel_id=panel_id)
        return owned_and_current

    def certificate(self, identity, panel_id, authorize):
        if self.certificate_deployment is None:
            raise ProvisioningNotStarted('证书部署未配置，未发出证书申请')

        return self.certificate_deployment.issue(
            identity, panel_id, self._certificate_authorization(identity, panel_id, authorize))

    def verify(self, identity, panel_id, authorize):
        owned_and_current = self._certificate_authorization(identity, panel_id, authorize)
        if self.certificate_deployment is not None:
            digest = self.certificate_deployment.deploy_existing(
                identity, panel_id, owned_and_current)
            if self.acceptance is not None:
                return self.acceptance.verify(identity, panel_id, digest, owned_and_current)
        raise ProvisioningError('HTTPS、应用路由及续期验收尚未完整接入，不能标记已接入')
