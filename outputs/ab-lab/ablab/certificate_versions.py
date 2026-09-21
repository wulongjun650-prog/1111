"""Append-only certificate publication, not a live Nginx deployment.

The privileged caller supplies the fixed base and holds the worker lock. Root
must own/control that tree; these checks do not isolate a malicious root peer.
Never repair an incomplete version automatically. A returned path is not an
HTTPS acceptance result and must be checked again before a future TLS switch.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import stat

from .certificates import CHAIN_LIMIT, KEY_LIMIT, _read_archive
from .nginx_entry import exclusive_text, no_symlinks, sync_directory
from .panel_sites import validate_identity
from .provisioning import ProvisioningError


def _authorized(enabled, authorize):
    if not enabled or authorize() is not True:
        raise ProvisioningError('证书版本写入未启用、已暂停或归属未确认')


def _private_directory(path):
    info = no_symlinks(path).lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('not a directory')
    if os.name != 'nt' and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('unsafe directory permissions')


def _record(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True) + '\n'


class CertificateVersions:
    def __init__(self, verifier, base_dir=Path('/var/lib/ab-lab-certificates'), *, writes_enabled=False):
        self.verifier = verifier
        self.base = Path(base_dir)
        self.writes_enabled = writes_enabled is True

    def publish(self, identity, panel_id, fullchain, private_key, authorize, *, now=None):
        validate_identity(identity)
        identity = dict(identity)
        if type(panel_id) is not int or panel_id <= 0:
            raise ValueError('面板站点 ID 无效')
        _authorized(self.writes_enabled, authorize)
        verified = self.verifier.verify(identity, 'production', fullchain, private_key, now=now)
        digest = sha256(verified.fullchain + b'\0' + verified.private_key).hexdigest()
        owner = dict(schema=1, identity=identity, panel_id=panel_id)
        ready = dict(owner, environment='production', version=digest,
                     fingerprint=verified.fingerprint, not_after=verified.not_after.isoformat())

        def directory(path):
            no_symlinks(path)
            created = False
            if not path.exists():
                _authorized(self.writes_enabled, authorize)
                path.mkdir(mode=0o700)  # No parents/exist_ok: races and foreign entries stop here.
                sync_directory(path.parent)
                created = True
            _private_directory(path)
            return created

        def write(path, text):
            _authorized(self.writes_enabled, authorize)
            _private_directory(path.parent)
            exclusive_text(path, text)

        def exact(path, expected, limit):
            if _read_archive(path, limit, private=True) != expected:
                raise ValueError('stored version differs')

        try:
            base = no_symlinks(self.base)
            _private_directory(base)  # Installer, not a web request, creates the root.
            environment = base / 'production'
            deployed = environment / 'deployed'
            site = deployed / ('ab-' + identity['site_id'])
            for parent in (environment, deployed):
                directory(parent)
            owner_text = _record(owner)
            if directory(site):
                write(site / 'owner.json', owner_text)
            exact(site / 'owner.json', owner_text.encode('ascii'), 8192)
            version = site / digest
            expected = {'fullchain.pem': (verified.fullchain, CHAIN_LIMIT),
                        'privkey.pem': (verified.private_key, KEY_LIMIT),
                        'ready.json': (_record(ready).encode('ascii'), 8192)}
            if directory(version):
                # Ready receipt is published last, after both fsynced PEM files.
                for name, (content, _) in expected.items():
                    write(version / name, content.decode('ascii'))
            if {entry.name for entry in version.iterdir()} != set(expected):
                raise ValueError('incomplete or foreign version')
            for name, (content, limit) in expected.items():
                exact(version / name, content, limit)
            for parent in (base, environment, deployed, site, version):
                _private_directory(parent)
            exact(site / 'owner.json', owner_text.encode('ascii'), 8192)
            _authorized(self.writes_enabled, authorize)
            sync_directory(version)
            return version
        except (OSError, ValueError):
            raise ProvisioningError('证书版本保存或核对失败，保留现场，禁止自动覆盖或修复') from None
