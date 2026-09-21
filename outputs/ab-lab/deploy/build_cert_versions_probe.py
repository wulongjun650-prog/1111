"""Emit an isolated Linux probe using current source and a generated test CA.

Needs cryptography already installed. Does not install dependencies, contact a
CA, import the app, or access any existing certificate/configuration directory.
Only creates and retains private /tmp/ab-lab-cert-* test trees.
"""
import ast
import base64
from hashlib import sha256
from pathlib import Path
import zlib

from build_cert_reader_probe import build_probe as reader_probe


def selected(path, names, *, decorators=True):
    source = path.read_text(encoding='utf-8')
    lines, parts, found = source.splitlines(), [], set()
    for node in ast.parse(source).body:
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in names:
            start = min([node.lineno] + [x.lineno for x in getattr(node, 'decorator_list', [])]) if decorators else node.lineno
            parts.append('\n'.join(lines[start - 1:node.end_lineno]))
            found.add(name)
    assert found == names, (path.name, names - found)
    return '\n\n'.join(parts) + '\n'


def build_probe():
    root = Path(__file__).resolve().parents[1]
    parts = [IMPORTS, reader_probe()]
    parts.append(selected(root/'ablab'/'nginx_entry.py', {'sync_directory', 'exclusive_text'}))
    parts.append(selected(root/'ablab'/'certificates.py', {
        'CERT_PEM', 'KEY_PEM', 'MATERIAL_ERRORS', '_certificates', 'VerifiedCertificate', 'CertificateVerifier'}))
    parts.append(selected(root/'ablab'/'certificate_versions.py', {
        '_authorized', '_private_directory', '_record', 'CertificateVersions'}))
    parts.append(selected(root/'tests'/'test_certificates.py', {
        'NOW', 'PEM', 'key_pem', 'make_cert', 'material'}, decorators=False))
    functions = '\n'.join(parts)
    return functions + '\nVERSION_SOURCE_HASH = ' + repr(sha256(functions.encode()).hexdigest()) + '\n' + PROBE


IMPORTS = '''
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import cryptography
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
'''


PROBE = r'''
assert os.name == 'posix'
version_root = Path(tempfile.mkdtemp(prefix='ab-lab-cert-versions-'))
m = material()
verify = CertificateVerifier(m['root'].public_bytes(PEM), m['stage'].public_bytes(PEM))
chain = m['leaf']().public_bytes(PEM) + m['intermediate'].public_bytes(PEM)
key = key_pem(m['key'])
def snapshot(path):
    return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in path.rglob('*') if p.is_file() and not p.is_symlink()}
def rejects(action):
    try: action()
    except ProvisioningError: return
    raise AssertionError('unsafe publication accepted')
completed = []
for case in ['valid', 'renewal', 'missing-ready', 'changed-key', 'hard-link', 'version-link',
             'base-mode', 'version-mode', 'key-mode', 'owner-mode', 'foreign-owner',
             'partial-write', 'pause', 'staging', 'default-disabled']:
    base = version_root / case
    base.mkdir(mode=0o700)
    store = CertificateVersions(verify, base, writes_enabled=case != 'default-disabled')
    def publish(**changes):
        args = dict(identity=identity, panel_id=22, fullchain=chain, private_key=key,
                    authorize=lambda: True, now=NOW)
        args.update(changes)
        return store.publish(**args)
    if case == 'default-disabled':
        rejects(publish)
        assert list(base.iterdir()) == []
    elif case == 'staging':
        stage = m['leaf'](issuer=m['stage'], issuer_key=m['stage_key']).public_bytes(PEM)
        rejects(lambda: publish(fullchain=stage))
        assert list(base.iterdir()) == []
    elif case == 'pause':
        rejects(lambda: publish(authorize=lambda: not list(base.rglob('fullchain.pem'))))
        assert list(base.rglob('fullchain.pem'))
        assert not list(base.rglob('privkey.pem')) and not list(base.rglob('ready.json'))
    else:
        version = publish()
        if case == 'valid':
            assert (version/'fullchain.pem').read_bytes() == chain
            assert (version/'privkey.pem').read_bytes() == key
            before = snapshot(base)
            assert publish() == version and snapshot(base) == before
            assert all(stat.S_IMODE(p.stat().st_mode) == (0o700 if p.is_dir() else 0o600)
                       for p in [base, *base.rglob('*')])
        elif case == 'renewal':
            before = snapshot(version)
            newer = m['leaf']().public_bytes(PEM) + m['intermediate'].public_bytes(PEM)
            new = publish(fullchain=newer)
            assert new != version and snapshot(version) == before
        elif case == 'partial-write':
            old = snapshot(version)
            newer = m['leaf']().public_bytes(PEM) + m['intermediate'].public_bytes(PEM)
            original_write = exclusive_text
            def interrupted(path, text, *args, **kwargs):
                if path.name == 'privkey.pem': raise OSError('injected write failure')
                return original_write(path, text, *args, **kwargs)
            exclusive_text = interrupted
            rejects(lambda: publish(fullchain=newer))
            exclusive_text = original_write
            before = snapshot(base)
            rejects(lambda: publish(fullchain=newer))
            assert snapshot(base) == before and snapshot(version) == old
            partial = [p for p in version.parent.iterdir() if p.is_dir() and p != version]
            assert len(partial) == 1 and not (partial[0]/'ready.json').exists()
        else:
            changes = {}
            if case == 'missing-ready': (version/'ready.json').unlink()
            elif case == 'changed-key': (version/'privkey.pem').write_bytes(b'changed fixture')
            elif case == 'hard-link': os.link(version/'privkey.pem', base/'copy')
            elif case == 'version-link':
                retained = version.with_name('retained-version')
                version.rename(retained); version.symlink_to(retained, target_is_directory=True)
            elif case == 'foreign-owner': changes = dict(identity=dict(identity, owner='c'*32))
            else:
                path = {'base-mode': base, 'version-mode': version,
                        'key-mode': version/'privkey.pem', 'owner-mode': version.parent/'owner.json'}[case]
                path.chmod(0o755 if path.is_dir() else 0o644)
            before = snapshot(base)
            rejects(lambda: publish(**changes))
            assert snapshot(base) == before
    completed.append(case)
print(json.dumps(dict(marker='AB_CERT_VERSIONS', source_sha256=VERSION_SOURCE_HASH,
                      root=str(version_root), cryptography=cryptography.__version__,
                      passed=len(completed), cases=completed), sort_keys=True))
'''


if __name__ == '__main__':
    payload = base64.b64encode(zlib.compress(build_probe().encode())).decode()
    print("python3 -c \"import base64,zlib; exec(zlib.decompress(base64.b64decode('" + payload + "')))\"")
