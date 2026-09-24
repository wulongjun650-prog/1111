"""Durable, staging-only renewal rehearsal proof.

The caller holds the provisioning lock. Intent markers are published before
each CA command. An uncertain dry-run is never repeated automatically because
Certbot does not save a result that can be reconciled after a crash.
"""
from pathlib import Path
import re
import stat

from .certificate_versions import _private_directory, _record
from .certificates import _read_archive
from .certbot_config import audit_private_config
from .nginx_entry import exclusive_text, no_symlinks, sync_directory
from .panel_sites import validate_identity
from .provisioning import ProvisioningError, ProvisioningNotStarted


STAGING_SERVER = 'https://acme-staging-v02.api.letsencrypt.org/directory'


class RenewalRehearsal:
    def __init__(self, command, state_dir, *,
                 writes_enabled=False, auditor=audit_private_config):
        if command.environment != 'staging':
            raise ValueError('续期演练只能使用 staging 证书账户')
        if not isinstance(command.account_id, str) or not re.fullmatch('[a-f0-9]{32}', command.account_id):
            raise ValueError('staging 证书账户 ID 无效')
        self.command = command
        self.root = no_symlinks(Path(state_dir))
        self.staging_root = no_symlinks(Path(command.root))
        self.writes_enabled = writes_enabled is True
        self.auditor = auditor

    def _authorized(self, authorize, *, launched=False):
        if not self.writes_enabled or authorize() is not True:
            error = '续期演练未启用、已暂停或归属未确认'
            if launched:
                raise ProvisioningError(error)
            raise ProvisioningNotStarted(error)

    def _record(self, identity, panel_id, digest, phase):
        return _record(dict(schema=1, identity=dict(identity), panel_id=panel_id,
                            production_version=digest, staging_account=self.command.account_id,
                            phase=phase))

    def _paths(self, identity, digest):
        prefix = identity['site_id'] + '-' + digest
        return {phase: no_symlinks(self.root / (prefix + '.' + phase.replace('_', '-') + '.json'))
                for phase in ('issue_intent', 'issued', 'dry_run_intent', 'verified')}

    def _exact(self, path, expected):
        try:
            return _read_archive(path, 8192, private=True) == expected.encode('ascii')
        except (OSError, ValueError):
            raise ProvisioningError('续期演练记录缺失、损坏或权限不安全') from None

    def _exists(self, path):
        try:
            info = no_symlinks(path).lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('not a regular file')
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            raise ProvisioningError('续期演练记录无法安全核对') from None

    def _publish(self, path, expected, authorize, *, launched=False):
        self._authorized(authorize, launched=launched)
        try:
            _private_directory(self.root)
            exclusive_text(path, expected)
            _private_directory(self.root)
        except (OSError, ValueError):
            raise ProvisioningError('续期演练记录写入结果不明确，保留现场') from None

    def _rollback(self, path, expected):
        try:
            if not self._exact(path, expected):
                raise ValueError('intent changed')
            path.unlink()
            sync_directory(path.parent)
        except (OSError, ValueError):
            raise ProvisioningError('未启动的续期命令无法安全回滚意图记录') from None

    def _lineage(self, identity):
        name = 'ab-' + identity['site_id'] + '.conf'
        path = no_symlinks(self.staging_root / 'config' / 'renewal' / name)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('not a regular file')
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            raise ProvisioningError('staging 续期配置无法安全核对') from None

    def _audit(self, identity):
        self.auditor(self.staging_root, identity,
                     self.command.account_id, STAGING_SERVER, operation='dry-run')

    def verify(self, identity, panel_id, digest, authorize):
        validate_identity(identity)
        if type(panel_id) is not int or panel_id <= 0:
            raise ValueError('面板站点 ID 无效')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('生产证书版本摘要无效')
        self._authorized(authorize)
        try:
            _private_directory(self.root)
        except (OSError, ValueError):
            raise ProvisioningNotStarted('续期演练私有目录不安全') from None

        paths = self._paths(identity, digest)
        records = {phase: self._record(identity, panel_id, digest, phase) for phase in paths}
        present = {phase: self._exists(path) for phase, path in paths.items()}

        if present['verified']:
            if not (present['issued'] and present['dry_run_intent']
                    and self._exact(paths['issued'], records['issued'])
                    and self._exact(paths['dry_run_intent'], records['dry_run_intent'])
                    and self._exact(paths['verified'], records['verified'])):
                raise ProvisioningError('续期演练收据链不完整或不一致')
            if present['issue_intent'] and not self._exact(paths['issue_intent'], records['issue_intent']):
                raise ProvisioningError('续期演练签发意图不一致')
            self._audit(identity)
            self._authorized(authorize, launched=True)
            return True

        if present['dry_run_intent']:
            if not present['issued'] or not self._exact(paths['issued'], records['issued']) \
                    or not self._exact(paths['dry_run_intent'], records['dry_run_intent']):
                raise ProvisioningError('续期演练意图链不完整或不一致')
            raise ProvisioningError('续期 dry-run 结果不明确，禁止自动重试，需人工核对')

        if present['issued']:
            if not self._exact(paths['issued'], records['issued']):
                raise ProvisioningError('staging lineage 收据不一致')
            if present['issue_intent'] and not self._exact(paths['issue_intent'], records['issue_intent']):
                raise ProvisioningError('staging 签发意图不一致')
            self._audit(identity)
        elif present['issue_intent']:
            if not self._exact(paths['issue_intent'], records['issue_intent']):
                raise ProvisioningError('staging 签发意图不一致')
            if not self._lineage(identity):
                raise ProvisioningError('staging 签发结果不明确且无可复核 lineage，禁止重试')
            self._audit(identity)
            self._publish(paths['issued'], records['issued'], authorize, launched=True)
        elif self._lineage(identity):
            self._audit(identity)
            self._publish(paths['issued'], records['issued'], authorize)
        else:
            self._publish(paths['issue_intent'], records['issue_intent'], authorize)
            try:
                self.command.run(identity, operation='issue', enabled=True, authorize=authorize)
            except ProvisioningNotStarted:
                self._rollback(paths['issue_intent'], records['issue_intent'])
                raise
            self._authorized(authorize, launched=True)
            self._audit(identity)
            self._publish(paths['issued'], records['issued'], authorize, launched=True)

        self._audit(identity)
        self._publish(paths['dry_run_intent'], records['dry_run_intent'], authorize)
        try:
            self.command.run(identity, operation='dry-run', enabled=True, authorize=authorize)
        except ProvisioningNotStarted:
            self._rollback(paths['dry_run_intent'], records['dry_run_intent'])
            raise
        self._authorized(authorize, launched=True)
        self._audit(identity)
        self._publish(paths['verified'], records['verified'], authorize, launched=True)
        return True
