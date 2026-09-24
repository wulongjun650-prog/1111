"""Durable, per-site production renewal with fail-closed crash recovery."""
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .certificate_versions import _private_directory, _record
from .certificates import _read_archive
from .nginx_entry import exclusive_text, no_symlinks, sync_directory
from .panel_sites import validate_identity
from .provisioning import ProvisioningError, ProvisioningNotStarted


class ProductionRenewal:
    def __init__(self, deployment, acceptance, state_dir, *,
                 writes_enabled=False, today=None):
        command = getattr(deployment, 'command', None)
        account_id = getattr(command, 'account_id', None)
        if (getattr(command, 'environment', None) != 'production'
                or not isinstance(account_id, str)
                or re.fullmatch(r'[0-9a-f]{32}', account_id) is None
                or getattr(deployment, 'versions', None) is None
                or getattr(acceptance, 'versions', None) is not deployment.versions):
            raise ValueError('生产续期组件未使用同一生产证书管线')
        self.deployment, self.acceptance, self.command = deployment, acceptance, command
        self.root = no_symlinks(Path(state_dir))
        self.writes_enabled = writes_enabled is True
        self.today = today or (lambda: datetime.now(timezone.utc).date())

    def _authorized(self, authorize, *, launched=False):
        if not self.writes_enabled or authorize() is not True:
            message = '生产续期未启用、已暂停或归属未确认'
            if launched:
                raise ProvisioningError(message)
            raise ProvisioningNotStarted(message)

    def _paths(self, identity):
        prefix = identity['site_id']
        return {
            name: no_symlinks(self.root / f'{prefix}.{name}.json')
            for name in ('intent', 'command', 'last')
        }

    def _exists(self, path):
        try:
            info = no_symlinks(path).lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('not a regular file')
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            raise ProvisioningError('生产续期记录无法安全核对') from None

    def _read(self, path):
        try:
            raw = _read_archive(path, 16 * 1024, private=True)
            value = json.loads(raw.decode('ascii'))
            if not isinstance(value, dict):
                raise ValueError('not an object')
            return value, raw.decode('ascii')
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise ProvisioningError('生产续期记录缺失、损坏或权限不安全') from None

    def _publish(self, path, value, authorize, *, launched=False):
        self._authorized(authorize, launched=launched)
        try:
            _private_directory(self.root)
            exclusive_text(path, _record(value))
            _private_directory(self.root)
        except (OSError, ValueError):
            raise ProvisioningError('生产续期记录写入结果不明确，保留现场') from None

    def _remove(self, path, expected):
        try:
            if _read_archive(path, 16 * 1024, private=True) != _record(expected).encode('ascii'):
                raise ValueError('record changed')
            path.unlink()
            sync_directory(path.parent)
        except (OSError, ValueError):
            raise ProvisioningError('生产续期记录清理结果不明确，保留现场') from None

    def _replace_last(self, path, text, expected):
        temporary = None
        try:
            _private_directory(self.root)
            expected_bytes = expected.encode('ascii')
            if _read_archive(path, 16 * 1024, private=True) != expected_bytes:
                raise ValueError('record changed')
            descriptor, name = tempfile.mkstemp(
                prefix='.ab-lab-renew-', suffix='.tmp', dir=self.root)
            temporary = Path(name)
            with os.fdopen(descriptor, 'w', encoding='ascii', newline='\n') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            if _read_archive(path, 16 * 1024, private=True) != expected_bytes:
                raise ValueError('record changed')
            os.replace(temporary, path)
            sync_directory(path.parent)
            if _read_archive(path, 16 * 1024, private=True) != text.encode('ascii'):
                raise ValueError('replacement mismatch')
        except (OSError, ValueError):
            raise ProvisioningError('生产续期最终记录写入结果不明确，保留现场') from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _base(self, identity, panel_id, cycle, phase):
        return dict(schema=1, identity=dict(identity), panel_id=panel_id,
                    production_account=self.command.account_id,
                    cycle=cycle, phase=phase)

    def _valid_final(self, value, identity, panel_id):
        proof = value.get('proof') if isinstance(value, dict) else None
        cycle = value.get('cycle') if isinstance(value, dict) else None
        try:
            valid_cycle = (isinstance(cycle, str)
                           and date.fromisoformat(cycle).isoformat() == cycle)
        except ValueError:
            valid_cycle = False
        version = value.get('version') if isinstance(value, dict) else None
        return (value.get('schema') == 1 and value.get('identity') == dict(identity)
                and value.get('panel_id') == panel_id
                and value.get('production_account') == self.command.account_id
                and value.get('phase') == 'verified'
                and valid_cycle
                and isinstance(version, str)
                and re.fullmatch(r'[0-9a-f]{64}', version) is not None
                and isinstance(proof, dict) and set(proof) == {'https', 'route', 'renewal'}
                and all(proof[key] is True for key in proof))

    def run(self, identity, panel_id, authorize):
        validate_identity(identity)
        identity = dict(identity)
        if type(panel_id) is not int or panel_id <= 0:
            raise ValueError('面板站点 ID 无效')
        self._authorized(authorize)
        try:
            _private_directory(self.root)
        except (OSError, ValueError):
            raise ProvisioningNotStarted('生产续期私有目录不安全') from None
        day = self.today()
        if type(day) is not date:
            raise ValueError('生产续期日期必须是 UTC date')
        cycle = day.isoformat()
        paths = self._paths(identity)

        last = last_text = None
        if self._exists(paths['last']):
            last, last_text = self._read(paths['last'])
            if not self._valid_final(last, identity, panel_id):
                raise ProvisioningError('生产续期最终记录与站点身份不一致')

        intent = self._base(identity, panel_id, cycle, 'intent')
        command = self._base(identity, panel_id, cycle, 'command_complete')
        intent_present = self._exists(paths['intent'])
        command_present = self._exists(paths['command'])

        # A verified final record makes interrupted residue cleanup safe.
        if last is not None and (intent_present or command_present):
            old_cycle = last['cycle']
            old_intent = self._base(identity, panel_id, old_cycle, 'intent')
            old_command = self._base(identity, panel_id, old_cycle, 'command_complete')
            stored_intent = self._read(paths['intent'])[0] if intent_present else None
            stored_command = self._read(paths['command'])[0] if command_present else None
            old_residue = ((stored_intent in (None, old_intent))
                           and (stored_command in (None, old_command)))
            current_residue = ((stored_intent in (None, intent))
                               and (stored_command in (None, command)))
            if old_residue:
                if intent_present:
                    self._remove(paths['intent'], old_intent)
                if command_present:
                    self._remove(paths['command'], old_command)
                intent_present = command_present = False
            elif not current_residue:
                raise ProvisioningError('生产续期残留记录跨周期或不一致，需人工核对')

        if last is not None and last['cycle'] == cycle:
            self._authorized(authorize, launched=True)
            return dict(last['proof'])
        if last is not None and last['cycle'] > cycle:
            raise ProvisioningError('系统日期早于生产续期记录，停止执行')

        if intent_present:
            stored, _ = self._read(paths['intent'])
            if stored != intent:
                raise ProvisioningError('存在其他周期或不一致的生产续期意图，需人工核对')
            if not command_present:
                raise ProvisioningError('生产续期命令结果不明确，禁止自动重试，需人工核对')
        elif command_present:
            raise ProvisioningError('生产续期命令记录缺少对应意图，需人工核对')
        else:
            self._publish(paths['intent'], intent, authorize)
            try:
                self.command.run(identity, operation='renew', enabled=True, authorize=authorize)
            except ProvisioningNotStarted:
                self._remove(paths['intent'], intent)
                raise
            self._authorized(authorize, launched=True)
            self._publish(paths['command'], command, authorize, launched=True)
            intent_present = command_present = True

        stored_command, _ = self._read(paths['command'])
        if stored_command != command:
            raise ProvisioningError('生产续期命令完成记录不一致')
        digest = self.deployment.deploy_existing(identity, panel_id, authorize)
        proof = self.acceptance.verify(identity, panel_id, digest, authorize)
        if (not isinstance(proof, dict) or set(proof) != {'https', 'route', 'renewal'}
                or any(proof[key] is not True for key in proof)):
            raise ProvisioningError('生产续期后的 HTTPS、路由或演练验收未全部通过')
        self._authorized(authorize, launched=True)
        final = dict(self._base(identity, panel_id, cycle, 'verified'),
                     version=digest, proof=dict(proof))
        try:
            if last is None:
                exclusive_text(paths['last'], _record(final))
            else:
                self._replace_last(paths['last'], _record(final), last_text)
        except (OSError, ValueError):
            raise ProvisioningError('生产续期最终记录写入结果不明确，保留现场') from None
        value, _ = self._read(paths['last'])
        if value != final:
            raise ProvisioningError('生产续期最终记录复核失败')
        self._remove(paths['intent'], intent)
        self._remove(paths['command'], command)
        return dict(proof)
