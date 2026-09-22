"""Read-only allowlist for our private Certbot configuration, not a manager.

Accept a deliberately narrow subset of ConfigObj syntax, not arbitrary INI.
Unsupported client output stops execution; it is never rewritten. Deployment
must check the installed client's output against this policy before enabling
commands. Caller holds the worker lock and controls all ancestors as root.
Certificate material/ownership validation remains a separate caller gate.
"""
from datetime import datetime
from pathlib import Path
import re

from .certificate_versions import _private_directory
from .certificates import _read_archive
from .nginx_entry import no_symlinks
from .panel_sites import validate_identity
from .provisioning import ProvisioningError


def _text(path, limit, *, allow_empty=False):
    text = _read_archive(path, limit, private=True, allow_empty=allow_empty).decode('ascii').replace('\r\n', '\n')
    if any(ord(char) < 32 and char not in '\t\n' or ord(char) == 127 for char in text):
        raise ValueError('ambiguous control character')
    return text


def _absent(path):
    try:
        no_symlinks(path).lstat()
    except FileNotFoundError:
        return
    raise ValueError('existing entry needs reconciliation')


def _sections(text):
    sections = {'lineage': {}}
    current = 'lineage'
    for raw in text.split('\n'):
        line = raw.strip(' \t\r')
        if not line or line.startswith('#'):
            continue
        if line in ('[renewalparams]', '[acme_renewal_info]', '[[webroot_map]]'):
            name = line.strip('[]')
            if (name in sections or (name == 'webroot_map' and current != 'renewalparams')
                    or (name == 'renewalparams' and current != 'lineage')
                    or (name == 'acme_renewal_info' and current != 'webroot_map')):
                raise ValueError('invalid section order')
            sections[name] = {}
            current = name
            continue
        match = re.fullmatch(r'([a-z0-9_.-]+)\s*=\s*([A-Za-z0-9_./:\- ,]+)', line)
        if match is None:
            raise ValueError('unsupported syntax')
        key, value = match.groups()
        if key in sections[current]:
            raise ValueError('duplicate key')
        sections[current][key] = value.strip()
    return sections


def _renewal(text, root, identity, account_id, server):
    sections = _sections(text)
    if set(sections) not in ({'lineage', 'renewalparams', 'webroot_map'},
                             {'lineage', 'renewalparams', 'webroot_map', 'acme_renewal_info'}):
        raise ValueError('missing sections')
    name = 'ab-' + identity['site_id']
    config = root / 'config'
    lineage = dict(sections['lineage'])
    if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', lineage.pop('version', '')) is None:
        raise ValueError('invalid version')
    expected = {kind: (config / 'live' / name / (kind + '.pem')).as_posix()
                for kind in ('cert', 'privkey', 'chain', 'fullchain')}
    expected['archive_dir'] = (config / 'archive' / name).as_posix()
    if lineage != expected:
        raise ValueError('unexpected lineage path or option')
    params = dict(sections['renewalparams'])
    required = {'authenticator': 'webroot', 'account': account_id, 'server': server}
    if any(params.pop(key, None) != value for key, value in required.items()):
        raise ValueError('foreign account, server or plugin')
    webroot = params.pop('webroot_path', None)
    if webroot not in (identity['path'], identity['path'] + ','):
        raise ValueError('foreign webroot')
    if sections['webroot_map'] != {identity['domain']: identity['path']}:
        raise ValueError('foreign webroot map')
    key_type = params.pop('key_type', None)
    if key_type not in ('rsa', 'ecdsa'):
        raise ValueError('unsupported key type')
    if 'rsa_key_size' in params:
        if key_type != 'rsa' or params.pop('rsa_key_size') not in ('2048', '3072', '4096'):
            raise ValueError('unsupported RSA size')
    if 'elliptic_curve' in params:
        if key_type != 'ecdsa' or params.pop('elliptic_curve') not in ('secp256r1', 'secp384r1'):
            raise ValueError('unsupported curve')
    for kind in ('config', 'work', 'logs'):
        if params.pop(kind + '_dir', (root / kind).as_posix()) != (root / kind).as_posix():
            raise ValueError('foreign directory')
    if params:
        raise ValueError('unsupported renewal option')
    if 'acme_renewal_info' in sections:
        ari = sections['acme_renewal_info']
        if set(ari) != {'ari_retry_after'}:
            raise ValueError('unsupported ARI option')
        value = ari['ari_retry_after']
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is not None or timestamp.isoformat(timespec='seconds') != value:
            raise ValueError('noncanonical ARI timestamp')


def audit_private_config(root, identity, account_id, server, *, operation):
    """Check only the specified lineage; never mutate or adopt existing data."""
    validate_identity(identity)
    if operation not in ('issue', 'renew', 'dry-run'):
        raise ValueError('证书操作无效')
    try:
        root = no_symlinks(Path(root))
        directories = (root.parent, root, root / 'config', root / 'work', root / 'logs',
                       root / 'config' / 'renewal')
        for directory in directories:
            _private_directory(directory)
        cli = _text(root / 'cli.ini', 8192, allow_empty=True)
        if any(line.strip() and not line.lstrip(' \t').startswith('#')
               for line in cli.split('\n')):
            raise ValueError('private CLI must contain comments only')
        name = 'ab-' + identity['site_id']
        renewal = root / 'config' / 'renewal' / (name + '.conf')
        _absent(renewal.with_name(renewal.name + '.new'))
        if operation == 'issue':
            for path in (renewal, root / 'config' / 'live' / name,
                         root / 'config' / 'archive' / name):
                _absent(path)
        else:
            _renewal(_text(renewal, 65536),
                     root, identity, account_id, server)
        for directory in directories:
            _private_directory(directory)
    except (OSError, ValueError):
        raise ProvisioningError('Certbot 私有配置未通过复核，停止执行；未覆盖或修复配置') from None
