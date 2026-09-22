"""Build a retained /tmp-only Linux probe. Never start or reload real Nginx.

Uses a newly generated test CA, existing cryptography and existing Nginx binary.
No installation, CA traffic, existing configuration or real private key reads.
The transaction's reload boundary is simulated; syntax/PEM loading is real.
"""
from hashlib import sha256
from pathlib import Path
import subprocess
import re

from build_cert_versions_probe import build_probe as versions_probe, selected


def private_listeners(text, prefix):
    """Only adapt known generated templates; never parse arbitrary site config."""
    path = Path(prefix).as_posix()
    if not re.fullmatch(r'[A-Za-z0-9_./:-]+', path):
        raise ValueError('unsafe probe prefix')
    def replace(match):
        original = match.group(1).split()
        if original == ['80']:
            return 'listen unix:' + path + '/http.sock;'
        if original == ['443', 'ssl']:
            return 'listen unix:' + path + '/https.sock ssl;'
        raise ValueError('unexpected listener in generated config')
    rendered, count = re.subn(r'\blisten\s+([^;]+);', replace, text)
    if not count:
        raise ValueError('missing explicit listener')
    return rendered


def empty_probe_pid(path):
    return not path.exists() or path.read_bytes() == b''


def nginx_check(prefix, config, *, runner=subprocess.run):
    prefix, config = Path(prefix).resolve(strict=True), Path(config).resolve(strict=True)
    if not config.is_relative_to(prefix) or not config.is_file():
        raise ValueError('probe config must stay in its private prefix')
    return runner(['/www/server/nginx/sbin/nginx', '-t', '-p', str(prefix) + '/',
                   '-c', str(config), '-e', str(prefix / 'error.log')],
                  shell=False, capture_output=True, text=True, timeout=15, check=False,
                  env={'PATH': '/usr/bin:/bin', 'HOME': str(prefix), 'LANG': 'C', 'LC_ALL': 'C'})


def build_probe():
    root = Path(__file__).resolve().parents[1]
    functions = '\n'.join([
        'import ctypes, sys, subprocess\n', versions_probe(),
        selected(root / 'ablab' / 'nginx_entry.py', {
            'read_bounded', 'replace_preserving', 'guarded_config', 'render_http_entry', 'NginxEntry'}),
        selected(root / 'ablab' / 'nginx_tls.py', {'render_tls_entry', 'TlsEntry'}),
        selected(Path(__file__), {'nginx_check', 'private_listeners', 'empty_probe_pid'}),
    ])
    return functions + '\nTLS_SOURCE_HASH = ' + repr(sha256(functions.encode()).hexdigest()) + '\n' + PROBE


PROBE = r'''
assert sys.platform == 'linux'
tls_root = Path(tempfile.mkdtemp(prefix='ab-lab-tls-probe-'))
load_cases = []
for case in ['valid', 'version-link', 'key-link', 'base-mode', 'version-mode', 'key-mode',
             'hardlink', 'ready', 'expired', 'foreign-owner', 'renamed']:
    base = tls_root / ('load-' + case)
    base.mkdir(mode=0o700)
    store = CertificateVersions(verify, base, writes_enabled=True)
    version = store.publish(identity, 22, chain, key, lambda: True, now=NOW)
    args = dict(identity=identity, panel_id=22, digest=version.name, now=NOW)
    if case == 'version-link':
        moved = version.with_name('retained'); version.rename(moved)
        version.symlink_to(moved, target_is_directory=True)
    elif case == 'key-link':
        moved = base/'retained-key'; (version/'privkey.pem').rename(moved)
        (version/'privkey.pem').symlink_to(moved)
    elif case in ('base-mode', 'version-mode', 'key-mode'):
        target = {'base-mode': base, 'version-mode': version, 'key-mode': version/'privkey.pem'}[case]
        target.chmod(0o755 if target.is_dir() else 0o644)
    elif case == 'hardlink': os.link(version/'privkey.pem', base/'linked-key')
    elif case == 'ready': (version/'ready.json').write_text('{}')
    elif case == 'expired': args['now'] = NOW + timedelta(days=100)
    elif case == 'foreign-owner': args['identity'] = dict(identity, owner='c'*32)
    elif case == 'renamed':
        args['digest'] = '0'*64; version.rename(version.with_name(args['digest']))
    before = snapshot(base)
    if case == 'valid':
        store.writes_enabled = False
        loaded, cert = store.load(**args)
        assert loaded == version and cert.fullchain == chain and cert.private_key == key
    else: rejects(lambda: store.load(**args))
    assert snapshot(base) == before
    load_cases.append(case)

configs = tls_root/'configs'; configs.mkdir(mode=0o700)
config = configs/(identity['domain']+'.conf')
config.write_text('server { listen 80; server_name new.example.com; }\n')
main_config = tls_root/'nginx.conf'
check_config = tls_root/'isolated-entry.conf'
main_config.write_text('pid '+str(tls_root/'nginx.pid')+';\n'
                      'error_log '+str(tls_root/'error.log')+';\n'
                      'events {}\nhttp { access_log off;\n' +
                      ''.join(kind+'_temp_path '+str(tls_root/kind)+';\n'
                              for kind in ['client_body', 'proxy', 'fastcgi', 'uwsgi', 'scgi']) +
                      'include '+str(check_config)+'; }\n')
def check_entry():
    # -t can bind sockets; never give it the production TCP listeners.
    check_config.write_text(private_listeners(config.read_text(), tls_root))
    return nginx_check(tls_root, main_config)
checks, simulated_reloads = [], []
inject_invalid = False
def probe_runner(args, **kwargs):
    assert args[0] == '/www/server/nginx/sbin/nginx'
    if args[1:] == ['-s', 'reload']:
        simulated_reloads.append(True)
        return subprocess.CompletedProcess(args, 0, '', '')
    assert args[1:] == ['-t']
    if inject_invalid and len(checks) % 2 == 1:
        return subprocess.CompletedProcess(args, 1, '', 'injected syntax failure')
    checked = check_entry()
    checks.append(checked.returncode)
    assert checked.returncode == 0, 'isolated nginx check failed; inspect private error.log'
    return checked

entry = NginxEntry(tls_root/'state', lambda *args: dict(identity, panel_id=22),
                   config_dir=configs, runner=probe_runner, writes_enabled=True)
entry.capture_created(identity, 22)
entry.configure(identity, 22)
cert_base = tls_root/'certificates'; cert_base.mkdir(mode=0o700)
store = CertificateVersions(verify, cert_base, writes_enabled=True)
version = store.publish(identity, 22, chain, key, lambda: True, now=NOW)
tls = TlsEntry(entry, store)
assert tls.configure(identity, 22, version.name, lambda: True, now=NOW)
assert 'listen 443 ssl;' in config.read_text()
count = len(simulated_reloads)
assert not tls.configure(identity, 22, version.name, lambda: True, now=NOW)
assert len(simulated_reloads) == count
old_files, old_config = snapshot(version), config.read_bytes()
new_chain = m['leaf'](end=NOW+timedelta(days=100)).public_bytes(PEM) + m['intermediate'].public_bytes(PEM)
new = store.publish(identity, 22, new_chain, key, lambda: True, now=NOW)
assert tls.configure(identity, 22, new.name, lambda: True, now=NOW)
assert snapshot(version) == old_files and new.as_posix() in config.read_text()
assert any(p.read_bytes() == old_config for p in configs.rglob('*') if p.is_file())

# Real nginx must reject a valid PEM belonging to the wrong test key.
candidate_key = new/'privkey.pem'
candidate_key.write_bytes(key_pem(m['root_key']))
wrong_key = check_entry()
assert wrong_key.returncode != 0, 'Nginx accepted mismatched key'
candidate_key.write_bytes(key)
assert check_entry().returncode == 0

previous_config = config.read_bytes()
next_chain = m['leaf'](end=NOW+timedelta(days=110)).public_bytes(PEM) + m['intermediate'].public_bytes(PEM)
next_version = store.publish(identity, 22, next_chain, key, lambda: True, now=NOW)
inject_invalid = True
count = len(simulated_reloads)
rejects(lambda: tls.configure(identity, 22, next_version.name, lambda: True, now=NOW))
assert config.read_bytes() == previous_config
assert (entry.root/(identity['site_id']+'.tls.pending')).exists()
rejects(lambda: tls.configure(identity, 22, next_version.name, lambda: True, now=NOW))
assert len(simulated_reloads) == count
assert check_entry().returncode == 0
assert empty_probe_pid(tls_root/'nginx.pid'), 'nginx test must not write a process ID'
print(json.dumps(dict(marker='AB_TLS_PROBE', source_sha256=TLS_SOURCE_HASH, root=str(tls_root),
                      load_pass=len(load_cases), nginx_valid_checks=len(checks)+2,
                      mismatched_key_rejected=True, transaction_pass=4,
                      reload_simulated=len(simulated_reloads), real_reload=False,
                      listeners='private-unix-sockets'), sort_keys=True))
'''


if __name__ == '__main__':
    print(build_probe())
