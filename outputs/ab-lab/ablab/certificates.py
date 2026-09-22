"""Offline validation of certificate bytes; no issuance or deployment side effects.

Trust anchors must come from privileged deployment configuration, never from
the candidate chain or the web console. Callers must deploy the verified bytes,
not re-read mutable Certbot live paths after validation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

from .panel_sites import validate_identity
from .nginx_entry import no_symlinks
from .provisioning import ProvisioningError


CHAIN_LIMIT = 256 * 1024
KEY_LIMIT = 16 * 1024
CERT_PEM = re.compile(rb'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----')
KEY_PEM = re.compile(rb'\s*-----BEGIN (PRIVATE KEY|EC PRIVATE KEY|RSA PRIVATE KEY)-----\s+'
                     rb'[A-Za-z0-9+/=\s]+-----END \1-----\s*')
MATERIAL_ERRORS = (ValueError, TypeError, UnsupportedAlgorithm, VerificationError,
                   x509.ExtensionNotFound, x509.DuplicateExtension)


def _certificates(pem, limit):
    if not isinstance(pem, bytes) or not 0 < len(pem) <= limit:
        raise ValueError('invalid PEM size')
    if CERT_PEM.sub(b'', pem).strip():
        raise ValueError('unexpected PEM data')
    certs = x509.load_pem_x509_certificates(pem)
    if not certs:
        raise ValueError('empty certificate set')
    return certs


@dataclass(frozen=True)
class VerifiedCertificate:
    domain: str
    environment: str
    fingerprint: str
    not_after: datetime
    fullchain: bytes = field(repr=False)
    private_key: bytes = field(repr=False)

    def require_production(self):
        if self.environment != 'production':
            raise ProvisioningError('测试证书不能部署到生产入口')


class CertificateVerifier:
    def __init__(self, production_roots, staging_roots):
        try:
            roots = [_certificates(pem, 2 * 1024 * 1024) for pem in (production_roots, staging_roots)]
            for group in roots:
                if not all(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                           for cert in group):
                    raise ValueError('non-CA trust anchor')
            # Reject cross-signed copies with the same key as well as exact duplicates.
            keys = [{cert.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
                     for cert in group} for group in roots]
            if keys[0] & keys[1]:
                raise ValueError('overlapping environment roots')
            self._stores = dict(zip(('production', 'staging'), map(Store, roots)))
        except MATERIAL_ERRORS:
            raise ProvisioningError('证书信任根无效或生产与测试根未隔离') from None

    def verify(self, identity, environment, fullchain, private_key, *, now=None):
        validate_identity(identity)
        if environment not in ('production', 'staging'):
            raise ValueError('证书环境无效')
        now = now if now is not None else datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('证书校验时间必须包含时区')
        try:
            certs = _certificates(fullchain, CHAIN_LIMIT)
            if (not isinstance(private_key, bytes) or not 0 < len(private_key) <= KEY_LIMIT
                    or KEY_PEM.fullmatch(private_key) is None):
                raise ValueError('invalid private key encoding')
            key = serialization.load_pem_private_key(private_key, password=None)
            leaf = certs[0]
            names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if list(names) != [x509.DNSName(identity['domain'])]:
                raise ValueError('SAN mismatch')
            format_args = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            if leaf.public_key().public_bytes(*format_args) != key.public_key().public_bytes(*format_args):
                raise ValueError('key mismatch')
            chain = (PolicyBuilder().store(self._stores[environment]).time(now).max_chain_depth(5)
                     .build_server_verifier(x509.DNSName(identity['domain'])).verify(leaf, certs[1:]))
            if certs != chain and certs != chain[:-1]:
                raise ValueError('unordered or unrelated certificates')
            return VerifiedCertificate(
                domain=identity['domain'], environment=environment,
                fingerprint=leaf.fingerprint(hashes.SHA256()).hex(), not_after=leaf.not_valid_after_utc,
                fullchain=b''.join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain[:-1]),
                private_key=key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        except MATERIAL_ERRORS:
            raise ProvisioningError('证书校验未通过：请核对域名、有效期、可信链和密钥') from None


def _file_snapshot(info, *, include_ctime=True):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns if include_ctime else None,
            info.st_mode, info.st_nlink)


def _read_archive(path, limit, *, private, allow_empty=False):
    before = no_symlinks(path).lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('not an exclusive regular file')
    if not (0 if allow_empty else 1) <= before.st_size <= limit:
        raise ValueError('material size invalid')
    if os.name != 'nt' and (before.st_uid != os.geteuid() or before.st_mode & (0o077 if private else 0o022)):
        raise ValueError('unsafe material permissions')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as source:
        opened = os.fstat(source.fileno())
        # Windows path stat and descriptor stat may expose different ctime
        # semantics after hard-link publication. Compare ctime within each API
        # below, not across APIs; retain every other cross-API identity check.
        compare_ctime = os.name != 'nt'
        if _file_snapshot(opened, include_ctime=compare_ctime) != _file_snapshot(before, include_ctime=compare_ctime):
            raise ValueError('material changed before open')
        content = source.read(limit + 1)
        if (_file_snapshot(os.fstat(source.fileno())) != _file_snapshot(opened)
                or _file_snapshot(no_symlinks(path).lstat()) != _file_snapshot(before)
                or len(content) != before.st_size):
            raise ValueError('material changed while reading')
    return content


def read_certbot_material(base_dir, environment, identity):
    """Read one owned lineage under a root-controlled base, with worker lock held.

    Only live's final symlinks are allowed. No filesystem writes. POSIX checks
    are enforced on Linux; Windows is a development platform, not deployment.
    """
    validate_identity(identity)
    if environment not in ('production', 'staging'):
        raise ValueError('证书环境无效')
    try:
        base = no_symlinks(Path(base_dir))
        name = 'ab-' + identity['site_id']
        config = base / environment / 'config'
        live = no_symlinks(config / 'live' / name)
        archive = no_symlinks(config / 'archive' / name)
        directories = {base, base / environment, config, live.parent, live, archive.parent, archive}
        for directory in directories:
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('not a directory')
            if os.name != 'nt' and (info.st_uid != os.geteuid()
                                   or info.st_mode & (0o077 if directory == base else 0o022)):
                raise ValueError('unsafe certificate directory')
        links, targets, generations = {}, {}, set()
        for kind in ('fullchain', 'privkey'):
            link = live / (kind + '.pem')
            # readlink refuses ordinary files and does not follow link chains.
            raw = os.readlink(link)
            target = no_symlinks(Path(os.path.abspath(link.parent / raw)))
            match = re.fullmatch(kind + r'([1-9][0-9]{0,9})\.pem', target.name)
            if target.parent != archive or match is None:
                raise ValueError('live target escaped lineage')
            links[link], targets[kind] = raw, target
            generations.add(match[1])
        if len(generations) != 1:
            raise ValueError('mixed archive generations')
        fullchain = _read_archive(targets['fullchain'], CHAIN_LIMIT, private=False)
        key = _read_archive(targets['privkey'], KEY_LIMIT, private=True)
        for link, raw in links.items():
            no_symlinks(link.parent)
            if os.readlink(link) != raw:
                raise ValueError('live target rotated while reading')
        return fullchain, key
    except (OSError, ValueError):
        raise ProvisioningError('证书文件不可安全读取：路径、权限、大小或归档版本异常') from None
