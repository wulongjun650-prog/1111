"""Compose certificate issuance and deployment; no account or HTTPS proof."""
from pathlib import Path

from .certificates import read_certbot_material
from .panel_sites import validate_identity
from .provisioning import ProvisioningError, ProvisioningNotStarted


CERTIFICATE_ROOT = Path('/var/lib/ab-lab-certificates')


class CertificateDeployment:
    def __init__(self, command, versions, tls, *, writes_enabled=False,
                 material_reader=read_certbot_material):
        if command.environment != 'production':
            raise ValueError('生产入口只能使用 production 证书账户')
        if tls.certificates is not versions:
            raise ValueError('TLS 与证书版本存储不一致')
        self.command, self.versions, self.tls = command, versions, tls
        self.writes_enabled = writes_enabled is True
        self.material_reader = material_reader

    def _authorized(self, identity, panel_id, authorize):
        validate_identity(identity)
        if type(panel_id) is not int or panel_id <= 0:
            raise ValueError('面板站点 ID 无效')
        if not self.writes_enabled or authorize() is not True:
            raise ProvisioningNotStarted('证书部署未启用、已暂停或归属未确认')

    def issue(self, identity, panel_id, authorize):
        self._authorized(identity, panel_id, authorize)
        self.command.run(identity, operation='issue', enabled=True, authorize=authorize)
        try:
            return self.deploy_existing(identity, panel_id, authorize)
        except ProvisioningNotStarted:
            raise ProvisioningError(
                '证书命令已完成但部署被暂停；保留签发意图，只核对现有材料，不重复申请') from None

    def deploy_existing(self, identity, panel_id, authorize):
        self._authorized(identity, panel_id, authorize)
        fullchain, private_key = self.material_reader(CERTIFICATE_ROOT, 'production', identity)
        version = self.versions.publish(identity, panel_id, fullchain, private_key, authorize)
        self.tls.configure(identity, panel_id, version.name, authorize)
        return version.name
