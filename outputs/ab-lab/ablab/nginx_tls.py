"""Opt-in TLS transaction for owned entries; not wired to the live worker.

Caller holds the provisioning lock and controls the private roots. Pending
transactions require manual reconciliation, including after an uncertain reload.
A successful return is not proof of an HTTPS handshake or renewal readiness.
"""
import json
import re

from .certificate_versions import _private_directory, _record
from .nginx_entry import (exclusive_text, guarded_config, no_symlinks,
                          read_bounded, render_http_entry, sync_directory)
from .panel_sites import validate_identity
from .provisioning import ProvisioningError


def render_tls_entry(identity, version_path):
    validate_identity(identity)
    path = no_symlinks(version_path).as_posix()
    if not re.fullmatch(r'[A-Za-z0-9_./:-]+', path):
        raise ValueError('证书路径含不允许的配置字符')
    # Reuse the HTTP entry's canonical proxy headers and ACME location.
    server = render_http_entry(identity).partition('server {')[2]
    server = ('server {' + server).replace('listen 80;', f'''listen 443 ssl;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_certificate "{path}/fullchain.pem";
    ssl_certificate_key "{path}/privkey.pem";''', 1)
    return f'''# AB Lab managed TLS entry {identity['site_id']}
server {{
    listen 80;
    server_name {identity['domain']};
    root {identity['path']};
    location ^~ /.well-known/acme-challenge/ {{
        default_type text/plain;
        try_files $uri =404;
    }}
    location / {{ return 308 https://{identity['domain']}$request_uri; }}
}}
''' + server


class TlsEntry:
    def __init__(self, nginx, certificates):
        self.nginx, self.certificates = nginx, certificates

    def configure(self, identity, panel_id, digest, authorize, *, now=None):
        identity = dict(identity)
        nginx = self.nginx

        def owned():
            nginx._owned(identity, panel_id)
            _private_directory(nginx.root)
            if authorize() is not True:
                raise ProvisioningError('HTTPS 配置已暂停或未获授权')

        owned()
        config, creation = nginx._paths(identity)
        state = no_symlinks(creation.with_suffix('.tls.json'))
        pending = no_symlinks(creation.with_suffix('.tls.pending'))
        config_pending = no_symlinks(creation.with_suffix('.tls-config.pending'))
        state_pending = no_symlinks(creation.with_suffix('.tls-state.pending'))
        if any(p.exists() for p in (creation.with_suffix('.pending'), pending, config_pending, state_pending)):
            raise ProvisioningError('存在未确认的配置事务，需人工核对，禁止自动重试')
        try:
            creation_text = read_bounded(creation, 2 * 1024 * 1024)
            receipt = json.loads(creation_text)
            http = render_http_entry(identity)
            if (receipt['identity'] != identity or receipt['panel_id'] != panel_id
                    or receipt['desired'] != http):
                raise ValueError('creation receipt mismatch')
            version, _ = self.certificates.load(identity, panel_id, digest, now=now)
            desired = render_tls_entry(identity, version)
            previous, state_text = http, None
            if state.exists():
                state_text = read_bounded(state)
                saved = json.loads(state_text)
                old_digest = saved['version']
                if not isinstance(old_digest, str) or not re.fullmatch('[0-9a-f]{64}', old_digest):
                    raise ValueError('invalid committed version')
                previous = render_tls_entry(identity, version.parent / old_digest)
                expected = dict(schema=1, identity=identity, panel_id=panel_id,
                                version=old_digest, desired=previous)
                if saved != expected:
                    raise ValueError('TLS receipt mismatch')

            def check(expected_config, expected_state=state_text):
                owned()
                if (read_bounded(config) != expected_config
                        or read_bounded(creation, 2 * 1024 * 1024) != creation_text
                        or (read_bounded(state) if state.exists() else None) != expected_state):
                    raise ProvisioningError('站点配置或事务记录已变化，停止切换')

            check(previous)
            if previous == desired:
                return False
            nginx._nginx('-t')
            self.certificates.load(identity, panel_id, digest, now=now)
            check(previous)
            intent = _record(dict(schema=1, identity=identity, panel_id=panel_id,
                                  version=digest, before=previous, after=desired))
            exclusive_text(pending, intent)
            check(previous)
            guarded_config(config, desired, previous, config_pending)
            try:
                nginx._nginx('-t')
            except ProvisioningError:
                # Only undo our own unchanged configuration, never an operator's edit.
                check(desired)
                guarded_config(config, previous, desired, config_pending)
                raise
            self.certificates.load(identity, panel_id, digest, now=now)
            check(desired)
            nginx._nginx('-s', 'reload')
            self.certificates.load(identity, panel_id, digest, now=now)
            check(desired)
            completed = _record(dict(schema=1, identity=identity, panel_id=panel_id,
                                     version=digest, desired=desired))
            if state_text is None:
                exclusive_text(state, completed)
            else:
                guarded_config(state, completed, state_text, state_pending)
            check(desired, completed)
            if read_bounded(pending) != intent:
                raise ValueError('transaction changed')
            pending.unlink()
            sync_directory(pending.parent)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            raise ProvisioningError('HTTPS 配置事务未完成，保留现场，需人工核对') from None
