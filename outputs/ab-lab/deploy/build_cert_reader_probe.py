"""Print a stdlib-only Linux probe built from the current production functions.

Run locally, then inspect/paste its command into an authorized Linux terminal.
The probe creates only a private /tmp/ab-lab-cert-read-* tree and retains it.
It neither imports the app nor loads real certificates or changes services.
"""
import ast
import base64
import hashlib
from pathlib import Path
import zlib


def build_probe():
    package = Path(__file__).resolve().parents[1] / 'ablab'
    parts = ['import os, stat, re, ipaddress, tempfile, json\nfrom pathlib import Path\n']
    for file, names in (
        ('provisioning.py', {'ProvisioningError'}),
        ('sites.py', {'normalize_domain'}),
        ('panel_sites.py', {'validate_identity'}),
        ('nginx_entry.py', {'no_symlinks'}),
        ('certificates.py', {'CHAIN_LIMIT', 'KEY_LIMIT', '_file_snapshot', '_read_archive', 'read_certbot_material'}),
    ):
        source = (package / file).read_text(encoding='utf-8')
        selected = set()
        for node in ast.parse(source).body:
            name = getattr(node, 'name', None)
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
            if name in names:
                parts.append(ast.get_source_segment(source, node) + '\n')
                selected.add(name)
        assert selected == names, (file, names - selected)
    functions = '\n'.join(parts)
    digest = hashlib.sha256(functions.encode()).hexdigest()
    return functions + '\nSOURCE_HASH = ' + repr(digest) + '\n' + PROBE


PROBE = r'''
assert os.name == 'posix', 'Linux probe only'
root = Path(tempfile.mkdtemp(prefix='ab-lab-cert-read-'))
identity = dict(site_id='a'*32, owner='b'*32, domain='new.example.com', path='/www/wwwroot/ab-lab-sites/'+'a'*32)
name = 'ab-' + identity['site_id']
def tree(case):
    base = root / case
    base.mkdir(mode=0o700)
    config = base / 'production' / 'config'
    live, archive = config/'live'/name, config/'archive'/name
    live.mkdir(parents=True)
    archive.mkdir(parents=True)
    for kind, value in [('fullchain', b'chain fixture'), ('privkey', b'key fixture')]:
        path = archive/(kind+'1.pem')
        path.write_bytes(value)
        path.chmod(0o600)
        (live/(kind+'.pem')).symlink_to(Path('../../archive')/name/path.name)
    return base, live, archive
results = []
for case in ['valid', 'foreign-site', 'foreign-environment', 'outside', 'mixed-generation',
             'target-link', 'plain-live', 'missing', 'directory', 'hard-link', 'fifo',
             'oversize-chain', 'oversize-key', 'empty-key', 'base-mode', 'key-mode',
             'archive-mode', 'rotation']:
    base, live, archive = tree(case)
    link, target = live/'privkey.pem', archive/'privkey1.pem'
    original_readlink = os.readlink
    if case in ('foreign-site', 'foreign-environment', 'outside'):
        other = (archive.parent/('ab-'+'f'*32)/'privkey1.pem' if case == 'foreign-site'
                 else base/'staging'/'config'/'archive'/name/'privkey1.pem' if case == 'foreign-environment'
                 else root/'outside-key.pem')
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b'foreign')
        other.chmod(0o600)
        link.unlink(); link.symlink_to(other)
    elif case in ('mixed-generation', 'target-link', 'rotation'):
        newer = archive/'privkey2.pem'
        newer.write_bytes(b'key fixture'); newer.chmod(0o600)
        if case == 'mixed-generation':
            link.unlink(); link.symlink_to(newer)
        elif case == 'target-link':
            target.unlink(); target.symlink_to(newer)
        else:
            rotated = []
            def rotate(path, *args, **kwargs):
                raw = original_readlink(path, *args, **kwargs)
                if Path(path) == link and not rotated:
                    rotated.append(True); link.unlink(); link.symlink_to(newer)
                return raw
            os.readlink = rotate
    elif case == 'plain-live':
        link.unlink(); link.write_bytes(b'key fixture')
    elif case in ('missing', 'directory', 'fifo'):
        target.unlink()
        if case == 'directory': target.mkdir()
        if case == 'fifo': os.mkfifo(target)
    elif case == 'hard-link': os.link(target, archive/'copy.pem')
    elif case == 'oversize-chain': (archive/'fullchain1.pem').write_bytes(b'x'*(CHAIN_LIMIT+1))
    elif case == 'oversize-key': target.write_bytes(b'x'*(KEY_LIMIT+1))
    elif case == 'empty-key': target.write_bytes(b'')
    elif case == 'base-mode': base.chmod(0o755)
    elif case == 'key-mode': target.chmod(0o644)
    elif case == 'archive-mode': archive.chmod(0o777)
    try:
        try:
            value = read_certbot_material(base, 'production', identity)
        except ProvisioningError:
            assert case != 'valid', 'valid fixture rejected'
        else:
            assert case == 'valid', 'unsafe fixture accepted: '+case
            assert value == (b'chain fixture', b'key fixture')
        results.append(case)
    finally:
        os.readlink = original_readlink
print(json.dumps(dict(marker='AB_CERT_READER', source_sha256=SOURCE_HASH, root=str(root),
                      passed=len(results), cases=results), sort_keys=True))
'''


if __name__ == '__main__':
    payload = base64.b64encode(zlib.compress(build_probe().encode())).decode()
    print("python3 -c \"import base64,zlib; exec(zlib.decompress(base64.b64decode('" + payload + "')))\"")
