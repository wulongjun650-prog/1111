import asyncio
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from typing import Annotated, Literal
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path as PathParameter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import maxminddb
from pydantic import Field

from .archives import EDITABLE_EXTENSIONS, MAX_FILE, MAX_ZIP, editable_file, import_content, read_source, resolve_file, source_files
from .analytics import summarize
from .models import ConfigUpdate, ContactLinkApply, ContactLinkInput, DirectEntry, LinkInput, PageTitleUpdate, RedirectApply, RedirectPresetInput, RedirectSplit, SourceEdit, StrictModel, TrackingApply, TrackingSnippetInput, VersionChoice, Visitor, WhatsAppNumberApply, WhatsAppNumberInput, WhatsAppReception, WhatsAppSplit
from .tracking import normalize_conversion, normalize_ga4
from .linkcheck import LinkChecker
from .watrust import PUBLIC_ORIGIN, TrustChecker, device_from_environ
from .campaign import build_campaign, export_events, fill_youtube_channels, window_bounds
from .flow import build_flow, intake_csv, summary_csv, visits_csv
from .contact_config import scan_contact
from .page_implant import implant_tree, scan_implant
from .redirects import has_whatsapp_builder, public_occurrences, rewrite_reception_bytes, rewrite_text, scan_bundle
from .rules import decide
from .cloudflare import Cloudflare
from .provisioning import ProvisioningError
from .reputation import GoogleReputation
from .speedcheck import SpeedBusy, SpeedCheckInput, SpeedChecker
from .store import Conflict, Store
from .auth import Auth, COOKIE, SESSION_SECONDS
from .sites import Registry

ROOT = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger('ablab')
SPLIT_COOKIE = 'ab_split'
WA_COOKIE = 'ab_wa'
_split_scans = {}
_split_scan_lock = threading.Lock()
Slot = Literal['A', 'B']
VersionId = Annotated[str, PathParameter(pattern=r'^[a-f0-9]{32}$')]


class SiteMetadata(StrictModel):
    note: str = Field(max_length=200, strict=True)


class AccountCreate(StrictModel):
    username: str = Field(min_length=1, max_length=64, strict=True)
    password: str = Field(min_length=12, max_length=256, strict=True)
    role: Literal['agent', 'observer'] = 'agent'


class AccountEnabled(StrictModel):
    enabled: bool = Field(strict=True)


class AccountPassword(StrictModel):
    password: str = Field(min_length=12, max_length=256, strict=True)


class SiteOwner(StrictModel):
    owner_id: str = Field(min_length=1, max_length=64, strict=True)


class MirrorBind(StrictModel):
    source_id: str = Field(default='', max_length=64, strict=True)
    observer_id: str = Field(default='', max_length=64, strict=True)


class VisitImport(StrictModel):
    source_id: str = Field(min_length=1, max_length=64, strict=True)


def preview_signature(store, account_store, site, expiry, version_id, issuer, local_only):
    # Account revocation and ownership transfers also revoke private drafts.
    # Target reads identity only; it must not initialize or migrate Auth tables.
    if issuer == 'local' and local_only:
        epoch = 0
    else:
        with account_store.connect() as db:
            account = db.execute('SELECT role,enabled,auth_epoch FROM accounts WHERE id=?', (issuer,)).fetchone()
        if not account or not account['enabled'] or (account['role'] != 'admin' and site['owner_id'] != issuer):
            return None
        epoch = account['auth_epoch']
    material = f"{expiry}:{version_id}:{site['id']}:{site['owner_id']}:{site['owner_epoch']}:{issuer}:{epoch}"
    return hmac.new(store.secret, material.encode(), hashlib.sha256).hexdigest()


class LocalBoundary:
    """Loopback only; public mode additionally requires explicit trusted proxy data."""
    def __init__(self, app, port, admin=False, csrf='', target_port=8766, deployment=None, auth=None, registry=None):
        self.app, self.port, self.admin, self.csrf = app, port, admin, csrf
        self.target_port = target_port
        self.deployment, self.auth = deployment, auth
        self.registry = registry

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('latin1')
        allowed_hosts = (f'127.0.0.1:{self.port}', f'localhost:{self.port}')
        peer = scope.get('client')
        try:
            local = bool(peer) and ipaddress.ip_address(peer[0]).is_loopback
        except ValueError:
            local = False
        origin = headers.get(b'origin')
        expected_origin = f'http://{host}'
        csrf = self.csrf
        state = scope.setdefault('state', {})
        site = None
        is_ingest = self.admin and scope['method'] == 'POST' and re.fullmatch(r'/api/wa-trust/screen/[A-Za-z0-9_-]{20,80}', scope['path']) is not None
        is_beacon = scope['method'] == 'POST' and scope['path'] == '/wa-events'
        if self.deployment:
            allowed_hosts = (self.deployment.host(self.admin),)
            if not self.admin and self.registry:
                site = self.registry.for_host(host)
                allowed_hosts = (host,) if site else ()
            expected_origin = self.deployment.admin_origin
            visitor_ip = restore_visitor_ip(headers.get(b'x-real-ip', b''), headers.get(b'cf-connecting-ip', b''))
            if visitor_ip is None:
                local = False
            else:
                state['visitor_ip'] = visitor_ip
            local &= headers.get(b'x-forwarded-proto') == b'https'
        invalid = not local or host not in allowed_hosts
        if self.admin and not is_beacon:
            invalid |= origin is not None and origin != expected_origin.encode()
            invalid |= headers.get(b'sec-fetch-site', b'none') not in (b'none', b'same-origin')
        if invalid:
            if is_beacon:
                await _record_rejected_beacon(self.registry, scope, receive)
            await JSONResponse({'detail': '来源、域名或代理校验失败'}, status_code=403)(scope, receive, send)
            return
        if not self.admin and self.registry:
            site = site or self.registry.get('default')
            # The original target origin also hosts signed, site-specific previews.
            # Local development alone permits full site access under this prefix.
            parts = scope['path'].split('/', 3)
            if len(parts) == 4 and parts[1] == '_sites':
                if self.deployment and (host != self.deployment.host(False) or not parts[3].startswith('_preview/')):
                    await JSONResponse({'detail': '资源不存在'}, status_code=404)(scope, receive, send)
                    return
                try:
                    site = self.registry.get(parts[2])
                except KeyError:
                    await JSONResponse({'detail': '站点不存在'}, status_code=404)(scope, receive, send)
                    return
                state['public_prefix'] = f'/_sites/{site["id"]}'
                scope = dict(scope, path='/' + parts[3])
            if not site['enabled']:
                await JSONResponse({'detail': '站点已暂停'}, status_code=503)(scope, receive, send)
                return
            state['site_id'] = site['id']
        if self.admin and self.auth:
            request = Request(scope)
            session = self.auth.session(request.cookies.get(COOKIE))
            public = is_beacon or is_ingest or (scope['path'] == '/api/login' and scope['method'] == 'POST') or (scope['method'] in ('GET', 'HEAD') and scope['path'] in ('/login', '/static/login.js', '/static/app.css'))
            if not public and session is None:
                response = RedirectResponse('/login', status_code=307) if scope['path'] == '/' else JSONResponse({'detail': '请先登录'}, status_code=401)
                response.headers['Cache-Control'] = 'no-store'
                await response(scope, receive, send)
                return
            if session and not public:
                csrf = session['csrf']
                state['username'] = session['username']
                state['account_id'] = session['account_id']
                state['role'] = session['role']
                # Reject before reading uploads or invoking route dependencies.
                match = re.match(r'^/api/sites/([^/]+)(?:/|$)', scope['path'])
                if match:
                    try:
                        owned = self.registry.get(match.group(1))
                        if session['role'] == 'observer':
                            if owned.get('observer_account') != session['account_id']:
                                raise KeyError(match.group(1))
                        elif session['role'] != 'admin' and owned['owner_id'] != session['account_id']:
                            raise KeyError(match.group(1))
                    except KeyError:
                        await JSONResponse({'detail': '站点不存在'}, status_code=404)(scope, receive, send)
                        return
                # The domains page refreshes Google's cached lookup. That write
                # stays inside the reputation cache and does not change a site.
                observer_may_refresh = scope['method'] == 'POST' and re.fullmatch(
                    r'/api/sites/(?:[a-f0-9]{32}|default)/reputation/check', scope['path'])
                # A speed check only asks Google and an external probe. It does not change a site.
                observer_may_probe = scope['method'] == 'POST' and scope['path'] == '/api/speedcheck'
                if (session['role'] == 'observer' and scope['method'] not in ('GET', 'HEAD')
                        and scope['path'] != '/api/logout' and not observer_may_refresh and not observer_may_probe):
                    await JSONResponse({'detail': '观察号只能查看'}, status_code=403)(scope, receive, send)
                    return
        state['csrf'] = csrf
        if self.admin and scope['method'] not in ('GET', 'HEAD') and not is_ingest and not is_beacon:
            if origin != expected_origin.encode() or not secrets.compare_digest(headers.get(b'x-csrf-token', b''), csrf.encode()):
                await JSONResponse({'detail': '请求校验失败，请刷新页面'}, status_code=403)(scope, receive, send)
                return
        is_upload = re.fullmatch(r'/api/(?:sites/[a-f0-9]{32}/|sites/default/)?upload/[AB]', scope['path'])
        limit = MAX_ZIP if self.admin and is_upload else 256 * 1024
        if is_ingest:
            limit = 2_000_000
        if self.admin and scope['method'] == 'POST' and re.fullmatch(r'/api/(?:sites/(?:[a-f0-9]{32}|default)/)?b-redirects/(?:apply|numbers/apply)', scope['path']):
            limit = 512 * 1024  # Up to 5,000 selected SHA-256 occurrence IDs.
        if self.admin and scope['method'] == 'POST' and re.fullmatch(r'/api/(?:sites/(?:[a-f0-9]{32}|default)/)?source/[AB]/[a-f0-9]{32}', scope['path']):
            # JSON can escape each UTF-8 byte as six ASCII characters.
            limit = MAX_FILE * 6 + 4096
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > limit:
                await JSONResponse({'detail': '请求体超过大小限制'}, status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get('more_body'):
                break
        pending = b''.join(chunks)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': pending, 'more_body': False}
            return await receive()

        async def secured(message):
            if message['type'] == 'http.response.start':
                extra = [(b'x-content-type-options', b'nosniff'), (b'cache-control', b'no-store'), (b'referrer-policy', b'no-referrer'), (b'permissions-policy', b'camera=(), microphone=(), geolocation=()')]
                if self.admin:
                    target_origin = self.deployment.target_origin if self.deployment else f'http://127.0.0.1:{self.target_port}'
                    policy = f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src {target_origin}; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
                    extra += [(b'content-security-policy', policy.encode()), (b'x-frame-options', b'DENY')]
                else:
                    # The page keeps its own origin so GA4 can store a cookie and send the visit.
                    # The admin is a different host, so this page still cannot read the admin session.
                    ancestors = self.deployment.admin_origin if self.deployment else 'http://127.0.0.1:8765 http://localhost:8765'
                    forms = 'https:' if self.deployment else f'https: http://127.0.0.1:{self.port}'
                    # Older landings assign https://api.whatsapp.com in this tab.
                    # Newer ones open https://wa.me in a new tab on desktop and
                    # set location to whatsapp:// on phones. Without the popup
                    # and custom-protocol flags, that click stays on the page.
                    # The new tab must leave this sandbox so WhatsApp Web is a
                    # normal page. This still cannot read the admin session.
                    policy = (
                        "sandbox allow-scripts allow-forms allow-same-origin "
                        "allow-popups allow-popups-to-escape-sandbox "
                        "allow-top-navigation-to-custom-protocols; "
                        f"frame-ancestors {ancestors}; form-action {forms}; object-src 'none'"
                    )
                    extra.append((b'content-security-policy', policy.encode()))
                message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() not in {key for key, _ in extra}] + extra
            await send(message)
        await self.app(scope, replay, secured)


# Published ranges from https://www.cloudflare.com/ips-v4 and /ips-v6.
# A client can set CF-Connecting-IP itself, so it counts only when the
# connecting address is one of these edges.
CLOUDFLARE_NETS = tuple(ipaddress.ip_network(item) for item in (
    '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22',
    '141.101.64.0/18', '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20',
    '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
    '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
    '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32',
    '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32',
))


def _address(value):
    try:
        text = value.decode('ascii').strip() if isinstance(value, (bytes, bytearray)) else str(value).strip()
        return ipaddress.ip_address(text)
    except (AttributeError, ValueError, UnicodeError):
        return None


def displayed_hong_kong(ip, country):
    """True when the visit list shows this row as Hong Kong, including old Japan-edge rows."""
    shown = shown_country(ip, country)
    return isinstance(shown, str) and shown.upper() == 'HK'


def shown_country(ip, country):
    """Old rows stored Cloudflare's Japan edge as a network. Show those as Hong Kong.

    A complete address keeps the country stored for that address.
    """
    if country != 'JP' or '/' not in str(ip or ''):
        return country
    try:
        network = ipaddress.ip_network(str(ip), strict=False)
    except (TypeError, ValueError):
        return country
    if any(network.version == block.version and network.subnet_of(block) for block in CLOUDFLARE_NETS):
        return 'HK'
    return country


def _present_logs(data):
    for item in data.get('items', []):
        item['country'] = shown_country(item.get('ip'), item.get('country'))
    return data


def restore_visitor_ip(edge_header, connecting_header):
    """Use the visitor address Cloudflare saw, not the Japan edge address."""
    edge = _address(edge_header)
    if edge is None:
        return None
    connecting = _address(connecting_header)
    if connecting is not None and connecting.is_global and any(edge in network for network in CLOUDFLARE_NETS):
        return str(connecting)
    return str(edge)


def geo_path():
    return os.environ.get('AB_GEOIP_PATH') or ROOT / 'resources' / 'country.mmdb'


def geo_country(ip):
    try:
        address = ipaddress.ip_address(ip)
        if not address.is_global or address.is_multicast:
            return None
        with maxminddb.open_database(geo_path()) as reader:
            row = reader.get(ip) or {}
            value = row.get('country', {}).get('iso_code')
            return value if isinstance(value, str) and len(value) == 2 and value.isascii() and value.isalpha() and value.isupper() and value not in ('XX', 'ZZ') else None
    except (OSError, ValueError, maxminddb.InvalidDatabaseError):
        return None


def geo_ready():
    return geo_status()['ready']


def geo_status():
    try:
        with maxminddb.open_database(geo_path()) as reader:
            metadata = reader.metadata()
            ready = any(word in metadata.database_type.lower() for word in ('country', 'city'))
            return {'ready': ready, 'reason': 'ready' if ready else 'unsupported', 'database': metadata.database_type[:80], 'built_at': metadata.build_epoch, 'bundled': not bool(os.environ.get('AB_GEOIP_PATH'))}
    except (OSError, ValueError, maxminddb.InvalidDatabaseError):
        return {'ready': False, 'reason': 'unavailable', 'database': '', 'built_at': None, 'bundled': not bool(os.environ.get('AB_GEOIP_PATH'))}


def base_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Conflict)
    async def conflict(_request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(_request, _error):
        return JSONResponse({'detail': '站点不存在'}, status_code=404)

    @app.exception_handler(Exception)
    async def failure(_request, error):
        event_id = secrets.token_hex(4)
        LOGGER.error('Internal failure %s (%s)', event_id, type(error).__name__)
        return JSONResponse({'detail': '内部错误，操作未确认完成', 'event_id': event_id}, status_code=500)
    return app


def create_admin(data_dir, port=8765, target_port=8766, deployment=None, registry_dir=None, server_ip='', cloudflare=None):
    if server_ip:
        from .provisioning import public_ipv4
        server_ip = public_ipv4(server_ip)
    app, store = base_app(), Store(data_dir)
    app.state.csrf = secrets.token_urlsafe(32)
    app.state.store = store
    registry = Registry(data_dir, deployment.host(False) if deployment else '', [deployment.host(True)] if deployment else [], registry_dir)
    app.state.registry = registry
    reputation = GoogleReputation(registry, os.environ.get('AB_GOOGLE_WEB_RISK_KEY', ''))
    app.state.reputation = reputation
    app.state.speedcheck = SpeedChecker()
    if cloudflare is None:
        cloudflare = Cloudflare(os.environ.get('AB_CLOUDFLARE_TOKEN', ''), os.environ.get('AB_CLOUDFLARE_TEMPLATE', ''), server_ip)
        if cloudflare.token:
            def watch_cloudflare_https():
                from .cloudflare import repair_strict_ssl
                while True:
                    try:
                        repair_strict_ssl(registry, cloudflare)
                    except Exception as error:
                        LOGGER.error('Cloudflare HTTPS repair failed (%s)', type(error).__name__)
                    time.sleep(60)
            threading.Thread(target=watch_cloudflare_https, name='cloudflare-https', daemon=True).start()
    app.state.cloudflare = cloudflare
    accounts = Auth(store)
    app.state.auth = accounts
    auth = accounts if deployment else None
    if auth and not auth.ready():
        raise ValueError('公网模式需要先在服务器终端设置管理员账号密码')
    target_url = deployment.target_origin if deployment else f'http://127.0.0.1:{target_port}'
    app.add_middleware(LocalBoundary, port=port, admin=True, csrf=app.state.csrf, target_port=target_port, deployment=deployment, auth=auth, registry=registry)
    templates = Jinja2Templates(directory=str(ROOT / 'templates'))
    app.mount('/static', StaticFiles(directory=str(ROOT / 'static'), check_dir=False), name='static')

    def principal(request):
        if auth:
            return accounts.get_account(request.state.account_id)
        return accounts.admin_account() or {'id': 'admin', 'username': '本地管理员', 'role': 'admin', 'enabled': True, 'created': 0}

    def sees_everything(account):
        return account['role'] == 'admin'

    def require_admin(request):
        account = principal(request)
        if account['role'] != 'admin':
            raise HTTPException(403, '需要总管理员权限')
        return account

    def authorized_site(request, site_id):
        site = registry.get(site_id)
        account = principal(request)
        if account['role'] == 'admin':
            return site
        if account['role'] == 'observer':
            if site.get('observer_account') != account['id']:
                raise HTTPException(404, '站点不存在')
            return site
        if site['owner_id'] != account['id']:
            raise HTTPException(404, '站点不存在')
        return site

    def site_store(request: Request):
        if 'site_id' not in request.path_params and principal(request)['role'] != 'admin':
            raise HTTPException(404, '请使用当前域名的操作入口')
        site_id = request.path_params.get('site_id', 'default')
        authorized_site(request, site_id)
        return registry.store(site_id)

    def records_of(site):
        """Visit rows for a mirror are read from its source. Kept copies stay on the observer domain."""
        mirror_store = registry.store(site['id'])
        source_id = (site.get('mirror_source') or '').strip()
        if not source_id:
            return mirror_store, False
        try:
            registry.get(source_id)
        except KeyError:
            return mirror_store, False
        source = registry.store(source_id)
        from .preserve import PreservedView, has_kept
        if has_kept(mirror_store):
            return PreservedView(source, mirror_store), True
        return source, True

    def source_domain_of(site):
        source_id = (site.get('mirror_source') or '').strip()
        if not source_id:
            return ''
        try:
            return registry.get(source_id).get('domain') or ''
        except KeyError:
            return ''

    def relabel_row(row, source_name, label):
        if source_name and source_name != label:
            for key, value in list(row.items()):
                if isinstance(value, str) and source_name in value:
                    row[key] = value.replace(source_name, label)
        return row

    def state(store, site_id):
        config, revision = store.config()
        site = registry.get(site_id)
        reading, _mirror = records_of(site)
        visit_url = target_url if site_id == 'default' else (f'https://{site["domain"]}' if deployment else f'{target_url}/_sites/{site_id}/')
        return {'config': config.model_dump(), 'revision': revision, 'versions': store.versions(), 'slots': store.slots(), 'links': store.links(), 'stats': reading.stats(), 'health': {'geoip': geo_ready(), 'geoip_detail': geo_status(), 'local_only': deployment is None}, 'site': site, 'target_url': visit_url, 'preview_origin': target_url, 'direct_mode': store.published_direct_mode()}

    def purge_site_cache(site):
        """Drop the cached landing page so a slot change is what visitors see."""
        if not site.get('cf_zone_id') or not cloudflare.configured:
            return None
        try:
            cloudflare.purge(site['cf_zone_id'], site['domain'])
        except ProvisioningError as error:
            return {'ok': False, 'detail': str(error)[:180]}
        return {'ok': True}

    def sync_followers(site_id):
        from .mirror import sync_mirror_content
        for mirror in registry.mirrors_of(site_id):
            try:
                changed = sync_mirror_content(registry.store(site_id), registry.store(mirror['id']))
            except Exception:
                LOGGER.exception('观察域名内容没有同步')
                continue
            if changed:
                purge_site_cache(mirror)

    def after_site_change(site_id):
        site = registry.get(site_id)
        purged = purge_site_cache(site)
        if not (site.get('mirror_source') or ''):
            sync_followers(site_id)
        return purged

    def guard_mirror(request):
        site_id = request.path_params.get('site_id', 'default')
        site = registry.get(site_id)
        if site.get('mirror_source'):
            raise HTTPException(400, '观察域名跟着投放域名，请在投放域名上修改')
        return site_id

    @app.get('/api/sites')
    def list_sites(request: Request):
        account = principal(request)
        if account['role'] == 'observer':
            bound = registry.site_for_observer(account['id'])
            rows = [bound] if bound else []
        elif account['role'] == 'admin':
            rows = registry.list()
        else:
            rows = registry.list(account['id'])
        sites = reputation.catalog(rows)
        if account['role'] == 'admin':
            names = {row['id']: row['username'] for row in accounts.list_accounts()}
            domains = {row['id']: row.get('domain') or '' for row in sites}
            for site in sites:
                site['owner_username'] = names.get(site['owner_id'], account['username'] if site['owner_id'] == 'admin' else '账号不存在')
                site['mirror_domain'] = domains.get(site.get('mirror_source') or '', '')
        else:
            for site in sites:
                site.pop('mirror_source', None)
                site.pop('observer_account', None)
        return {'sites': sites, 'account': account, 'google_reputation_configured': reputation.configured,
                'cloudflare_configured': cloudflare.configured, 'cloudflare_template': cloudflare.template_label,
                'server_ip': server_ip, 'record_type': 'A', 'local_only': deployment is None}

    @app.get('/api/me')
    def whoami(request: Request):
        return {'account': principal(request)}

    @app.get('/api/accounts')
    def list_accounts(request: Request):
        account = principal(request)
        if not sees_everything(account):
            raise HTTPException(403, '需要总管理员权限')
        counts = {}
        for site in registry.list():
            counts[site['owner_id']] = counts.get(site['owner_id'], 0) + 1
        return {'items': [account | {'domain_count': counts.get(account['id'], 0)} for account in accounts.list_accounts()]}

    @app.post('/api/accounts')
    def create_account(request: Request, body: AccountCreate):
        require_admin(request)
        created = accounts.create_observer(body.username, body.password) if body.role == 'observer' else accounts.create_agent(body.username, body.password)
        return {'account': created}

    @app.patch('/api/accounts/{account_id}')
    def enable_account(account_id: str, request: Request, body: AccountEnabled):
        require_admin(request)
        return {'account': accounts.set_enabled(account_id, body.enabled)}

    @app.post('/api/accounts/{account_id}/password')
    def reset_account_password(account_id: str, request: Request, body: AccountPassword):
        require_admin(request)
        accounts.reset_agent_password(account_id, body.password)
        return {'ok': True}

    @app.put('/api/sites/{site_id}/owner')
    def assign_site_owner(site_id: str, request: Request, body: SiteOwner):
        require_admin(request)
        target = accounts.get_account(body.owner_id)
        if target['role'] not in ('admin', 'agent'):
            raise ValueError('观察号不能作为域名归属')
        return {'site': registry.assign_owner(site_id, body.owner_id)}

    @app.put('/api/sites/{site_id}/mirror')
    def bind_site_mirror(site_id: str, request: Request, body: MirrorBind):
        require_admin(request)
        registry.get(site_id)
        if body.observer_id:
            account = accounts.get_account(body.observer_id)
            if account['role'] != 'observer':
                raise ValueError('只能把观察号绑到观察域名')
            if not account['enabled']:
                raise ValueError('观察号已停用')
        site = registry.bind_mirror(site_id, body.source_id, body.observer_id)
        if site.get('mirror_source'):
            try:
                from .mirror import sync_mirror_content
                if sync_mirror_content(registry.store(site['mirror_source']), registry.store(site['id'])):
                    purge_site_cache(site)
            except Exception:
                LOGGER.exception('观察域名内容没有同步')
        site = registry.get(site_id)
        try:
            site['mirror_domain'] = registry.get(site['mirror_source']).get('domain') or '' if site.get('mirror_source') else ''
        except KeyError:
            site['mirror_domain'] = ''
        return {'site': site}

    @app.post('/api/sites/{site_id}/follow')
    def follow_with_observer(site_id: str, request: Request):
        """Point the existing observer domain at this site. Visit rows stay where they are."""
        require_admin(request)
        source = registry.get(site_id)
        if (source.get('mirror_source') or '').strip():
            raise ValueError('这是观察域名自己。请先选中要跟着的投放域名，再点设置更寻。')
        observers = [site for site in registry.list() if (site.get('observer_account') or '').strip()]
        if not observers:
            raise ValueError('还没有观察域名。请先在域名列表把一个域名设为观察域名。')
        from .mirror import sync_mirror_content
        followed = []
        sync_failed = False
        for observer in observers:
            try:
                account = accounts.get_account(observer['observer_account'])
            except KeyError:
                raise ValueError('观察号已经不在了，请重新把域名设为观察域名') from None
            if account['role'] != 'observer' or not account['enabled']:
                raise ValueError('观察号已停用，不能设置更寻')
            registry.bind_mirror(observer['id'], site_id, observer['observer_account'])
            synced = False
            try:
                synced = bool(sync_mirror_content(registry.store(site_id), registry.store(observer['id'])))
            except Exception:
                LOGGER.exception('观察域名内容没有同步')
                sync_failed = True
            else:
                if synced:
                    purge_site_cache(registry.get(observer['id']))
            site = registry.get(observer['id'])
            followed.append({'id': site['id'], 'domain': site.get('domain') or '', 'synced': synced})
        names = '、'.join(item['domain'] or item['id'] for item in followed)
        source_domain = source.get('domain') or site_id
        if sync_failed:
            message = f'观察域名 {names} 已改成跟着 {source_domain}，但内容没有同步上。以前的记录都还在。'
        else:
            message = f'观察域名 {names} 已跟着 {source_domain}。以前的记录都还在。'
        return {'source_id': site_id, 'source_domain': source_domain, 'observers': followed, 'sync_failed': sync_failed, 'message': message}

    @app.post('/api/sites/{site_id}/import-records')
    def import_site_records(site_id: str, request: Request, body: VisitImport):
        require_admin(request)
        observer = registry.get(site_id)
        if not (observer.get('observer_account') or '').strip():
            raise ValueError('请选择观察域名。先在域名列表把一个域名设为观察域名。')
        if body.source_id == site_id:
            raise ValueError('来源域名和观察域名要选两个不同的域名。')
        source = registry.get(body.source_id)
        from .preserve import import_visit_records
        counts = import_visit_records(
            registry.store(source['id']), registry.store(observer['id']),
            source.get('domain') or '', observer.get('domain') or '',
        )
        observer = registry.get(site_id)
        source_domain = source.get('domain') or '原有站点'
        observer_domain = observer.get('domain') or '观察域名'
        labels = (('events', '访问记录'), ('access_log', '请求记录'), ('client_events', '落地页事件'), ('intake_log', '原始接收'))
        parts = [f'{label} {counts[key]} 条' for key, label in labels if counts.get(key)]
        if parts:
            message = f'已把 {source_domain} 的数据导入 {observer_domain}。{"，".join(parts)}。再导一次会再叠一份。设置更寻保持原样。'
        else:
            message = f'{source_domain} 没有访问记录，没有导入。'
        return {
            'source_id': source['id'], 'source_domain': source_domain,
            'observer_id': observer['id'], 'observer_domain': observer_domain,
            'mirror_source': observer.get('mirror_source') or '',
            'counts': counts, 'imported': sum(counts.values()), 'message': message,
        }

    @app.post('/api/sites/{site_id}/reputation/check')
    def check_site_reputation(site_id: str, request: Request):
        authorized_site(request, site_id)
        return {'result': reputation.check(site_id)}

    def apply_cloudflare(site):
        try:
            attached = cloudflare.attach(site['domain'])
        except ProvisioningError as error:
            names = ','.join(getattr(error, 'nameservers', ()) or ())
            zone_id = getattr(error, 'zone_id', '') or ''
            detail = str(error)[:400]
            updated = registry.set_cloudflare(site['id'], zone_id, names, 'failed', detail)
            if zone_id and site['stage'] != 'active':
                registry.transition(updated['id'], updated['generation'], 'waiting_dns', detail)
                updated = registry.get(site['id'])
            return updated, {'ok': False, 'detail': detail, 'nameservers': names.split(',') if names else [], 'status': 'failed'}
        names = ','.join(attached['nameservers'])
        updated = registry.set_cloudflare(site['id'], attached['zone_id'], names, attached['status'], attached['detail'])
        if site['stage'] != 'active':
            if attached['status'] == 'active' and registry.transition(updated['id'], updated['generation'], 'dns_verified', attached['detail']):
                registry.transition(updated['id'], updated['generation'], 'unsupported', '解析条件已满足；面板自动写入适配尚未完成，未创建目录、站点或证书')
            else:
                registry.transition(updated['id'], updated['generation'], 'waiting_dns', attached['detail'])
        return registry.get(site['id']), {
            'ok': True, 'detail': attached['detail'], 'nameservers': attached['nameservers'], 'status': attached['status'],
        }

    @app.post('/api/sites')
    def add_site(body: dict, request: Request):
        if not isinstance(body, dict) or set(body) - {'domain', 'cloudflare'} or 'domain' not in body:
            raise ValueError('只需提交域名，可另选是否套用 Cloudflare')
        use_cloudflare = body.get('cloudflare', False)
        if type(use_cloudflare) is not bool:
            raise ValueError('Cloudflare 选项无效')
        if use_cloudflare and not cloudflare.configured:
            raise ValueError('服务器未配置 Cloudflare，请取消勾选，按原来的 A 记录接入')
        if use_cloudflare and not cloudflare.origin_ip:
            raise ValueError('服务器公网 IP 未配置，无法把 Cloudflare 回源到本机')
        site = registry.add(body['domain'], principal(request)['id'])
        result = {'site': site, 'server_ip': server_ip, 'record_type': 'A', 'cloudflare': None}
        if use_cloudflare:
            result['site'], result['cloudflare'] = apply_cloudflare(site)
        return result

    @app.post('/api/sites/{site_id}/cloudflare')
    def retry_cloudflare(site_id: str, request: Request):
        site = authorized_site(request, site_id)
        if site['id'] == 'default':
            raise ValueError('原有站点不在这里套用 Cloudflare')
        if not cloudflare.configured:
            raise ValueError('服务器未配置 Cloudflare')
        if not cloudflare.origin_ip:
            raise ValueError('服务器公网 IP 未配置，无法把 Cloudflare 回源到本机')
        if not site['cf_status'] and site['stage'] != 'active':
            raise ValueError('请先等本机证书接入完成，再套用 Cloudflare')
        site, attached = apply_cloudflare(site)
        return {'site': site, 'cloudflare': attached}

    @app.post('/api/sites/{site_id}/cloudflare/status')
    def cloudflare_status(site_id: str, request: Request):
        site = authorized_site(request, site_id)
        if not site['cf_zone_id']:
            raise ValueError('这个域名没有接入 Cloudflare')
        if not cloudflare.configured:
            raise ValueError('服务器未配置 Cloudflare')
        try:
            state = cloudflare.confirm(site['cf_zone_id'], site['domain'])
        except ProvisioningError as error:
            detail = str(error)[:400]
            names = [part for part in site['cf_nameservers'].split(',') if part]
            return {'site': site, 'cloudflare': {'ok': False, 'status': site['cf_status'] or 'pending', 'active': False, 'nameservers': names, 'detail': detail}}
        status = 'active' if state['active'] else 'pending'
        names = ','.join(state['nameservers']) or site['cf_nameservers']
        if status == 'active':
            detail = 'Cloudflare 已生效，回源指向服务器'
        else:
            shown = '、'.join(state['nameservers']) or site['cf_nameservers'].replace(',', '、')
            detail = f'NS 尚未生效，正在自动检查。请到注册商改为：{shown}'[:400]
        if (site['cf_status'], site['cf_nameservers'], site['cf_detail']) != (status, names, detail):
            site = registry.set_cloudflare(site['id'], site['cf_zone_id'], names, status, detail)
        if status == 'active' and site['stage'] == 'waiting_dns':
            if registry.transition(site['id'], site['generation'], 'dns_verified', detail):
                registry.transition(site['id'], site['generation'], 'unsupported', '解析条件已满足；面板自动写入适配尚未完成，未创建目录、站点或证书')
            site = registry.get(site['id'])
        return {'site': site, 'cloudflare': {'ok': True, 'status': status, 'active': status == 'active', 'nameservers': [part for part in names.split(',') if part], 'detail': detail}}

    @app.post('/api/sites/{site_id}/cloudflare/disable')
    def disable_cloudflare(site_id: str, request: Request):
        site = authorized_site(request, site_id)
        if site['id'] == 'default':
            raise ValueError('原有站点不在这里关闭 Cloudflare')
        if not site['cf_zone_id'] and not site['cf_status']:
            raise ValueError('这个域名没有接入 Cloudflare')
        record_address = ''
        if site['cf_zone_id']:
            if not cloudflare.configured:
                raise ValueError('服务器未配置 Cloudflare')
            turned = cloudflare.disable(site['cf_zone_id'], site['domain'])
            record_address = turned['address']
        if site['stage'] in ('active', 'legacy', 'paused'):
            detail = '已关闭 Cloudflare 代理。访客将直接访问 A 记录。本机站点保持不变。'
        else:
            target = server_ip or '本机公网 IP'
            detail = f'已关闭 Cloudflare 代理。请把 A 记录指到 {target}，指向本机后会自动建站并申请证书。'
        updated = registry.clear_cloudflare(site['id'], detail)
        return {
            'site': updated, 'server_ip': server_ip, 'record_address': record_address,
            'cloudflare': {'ok': True, 'proxied': False, 'detail': detail},
        }

    @app.post('/api/sites/{site_id}/cloudflare/purge')
    def purge_cloudflare(site_id: str, request: Request):
        site = authorized_site(request, site_id)
        if not site['cf_zone_id']:
            raise ValueError('这个域名没有接入 Cloudflare')
        if not cloudflare.configured:
            raise ValueError('服务器未配置 Cloudflare')
        cloudflare.purge(site['cf_zone_id'], site['domain'])
        for mirror in registry.mirrors_of(site_id):
            purge_site_cache(mirror)
        return {'ok': True}

    @app.patch('/api/sites/{site_id}/metadata')
    def update_site_metadata(site_id: str, body: SiteMetadata, request: Request):
        authorized_site(request, site_id)
        return {'site': registry.set_note(site_id, body.note)}

    @app.post('/api/sites/{site_id}/availability/{action}')
    def update_site_availability(site_id: str, action: Literal['online', 'offline'], request: Request):
        authorized_site(request, site_id)
        return {'site': registry.set_availability(site_id, action)}

    @app.delete('/api/sites/{site_id}')
    def delete_site(site_id: str, request: Request, keep_records: bool = Query(default=False)):
        require_admin(request)
        if site_id == 'default':
            raise ValueError('原有站点不能删除')
        site = registry.get(site_id)
        kept = []
        if keep_records:
            if (site.get('observer_account') or '').strip() or (site.get('mirror_source') or '').strip():
                raise ValueError('这是观察域名。它自己的记录会跟着删除，不能再保留到观察域名。')
            observers = [item for item in registry.list() if (item.get('observer_account') or '').strip()]
            if not observers:
                raise ValueError('还没有观察域名，访问记录没法保留。')
            from .preserve import keep_visit_records
            source_store = registry.store(site_id)
            source_domain = site.get('domain') or ''
            for observer in observers:
                counts = keep_visit_records(source_store, registry.store(observer['id']), source_domain, observer.get('domain') or '')
                kept.append({'domain': observer.get('domain') or '', **counts})
        registry.detach_source(site_id)
        snapshot = registry.remove(site_id)
        warning = ''
        zone_id = snapshot.get('cf_zone_id') or ''
        if zone_id:
            try:
                cloudflare.delete_zone(zone_id, snapshot['domain'])
                zone_id = ''
            except ProvisioningError as error:
                warning = str(error)[:180]
        from .site_erasure import queue_erasure
        queue_erasure(registry.root, dict(snapshot, cf_zone_id=zone_id))
        if snapshot.get('_wipe_failed'):
            warning = (warning + ' ' if warning else '') + '部分文件将由服务器继续清理。'
        return {'ok': True, 'domain': snapshot['domain'], 'cloudflare_warning': warning, 'kept': kept}

    @app.post('/api/sites/{site_id}/provision/{action}')
    def provision_control(site_id: str, action: Literal['pause', 'retry'], request: Request):
        authorized_site(request, site_id)
        return {'site': registry.control(site_id, action)}

    @app.get('/api/sites/{site_id}/provision/events')
    def provision_events(site_id: str, request: Request):
        authorized_site(request, site_id)
        return {'items': registry.events(site_id)}

    routes = APIRouter()

    @app.get('/', response_class=HTMLResponse)
    def index(request: Request):
        account = principal(request)
        return templates.TemplateResponse(request=request, name='index.html', context={'csrf_token': request.state.csrf, 'target_url': target_url, 'deployment': bool(deployment), 'username': account['username'], 'account': account})

    if auth:
        @app.get('/login', response_class=HTMLResponse)
        def login_page(request: Request):
            return templates.TemplateResponse(request=request, name='login.html', context={'csrf_token': app.state.csrf})

        @app.post('/api/login')
        def login(body: dict, request: Request):
            token, status = auth.login(body.get('username'), body.get('password'), request.state.visitor_ip)
            if token is None:
                return JSONResponse({'detail': '账号或密码错误'}, status_code=status)
            response = JSONResponse({'ok': True, 'csrf': auth.session(token)['csrf']})
            response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite='strict', path='/')
            return response

        @app.post('/api/logout')
        def logout(request: Request):
            auth.logout(request.cookies[COOKIE])
            response = JSONResponse({'ok': True})
            response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
            return response

    @routes.get('/state')
    def get_state(request: Request, store=Depends(site_store)):
        payload = state(store, request.path_params.get('site_id', 'default'))
        if principal(request)['role'] == 'observer':
            shown = dict(payload['site'])
            shown.pop('mirror_source', None)
            shown.pop('observer_account', None)
            payload['site'] = shown
        return payload

    @routes.get('/analytics')
    def analytics(request: Request, period: Literal['today', 'yesterday', '7d', '30d'] = '7d',
                  scope: Literal['current', 'all'] = 'current',
                  tz_offset: int = Query(default=480, ge=-840, le=840)):
        site_id = request.path_params.get('site_id', 'default')
        account = principal(request)
        if account['role'] == 'observer':
            bound = registry.site_for_observer(account['id'])
            if bound is None:
                return summarize(registry, 'default', period, 'all', tz_offset, time.time(), '__no_observer_site__')
            if 'site_id' in request.path_params and site_id != bound['id']:
                raise HTTPException(404, '站点不存在')
            return summarize(registry, bound['id'], period, 'current', tz_offset, time.time(), None)
        everything = sees_everything(account)
        if scope != 'all' and 'site_id' not in request.path_params and not everything:
            raise HTTPException(404, '请使用当前域名的操作入口')
        if scope != 'all' or 'site_id' in request.path_params:
            authorized_site(request, site_id)
        return summarize(registry, site_id, period, scope, tz_offset, time.time(), None if everything else account['id'])

    @routes.put('/config')
    def update_config(body: ConfigUpdate, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        store.save_config(body.config, body.revision)
        result = state(store, site_id)
        purged = after_site_change(site_id)
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.post('/upload/{slot}')
    async def upload(slot: Slot, request: Request, name: str = Query(min_length=1, max_length=200), store=Depends(site_store)):
        guard_mirror(request)
        data = await request.body()
        # CPU/disk work in worker thread keeps health and UI requests responsive.
        from starlette.concurrency import run_in_threadpool
        version = await run_in_threadpool(import_content, data, name, store.pages)
        implant = None
        if slot == 'B':
            root = store.pages / version['id']
            implant = await run_in_threadpool(implant_tree, root)
            version['bytes'] = sum(item.stat().st_size for item in root.rglob('*') if item.is_file())
        await run_in_threadpool(store.add_version, slot, version)
        return {'version': version | {'slot': slot}, 'implant': implant}

    @routes.post('/publish/{slot}')
    def publish(slot: Slot, body: VersionChoice, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        store.publish(slot, body.version_id)
        result = {'ok': True}
        purged = after_site_change(site_id)
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.delete('/versions/{slot}/{version_id}')
    def delete_slot_version(slot: Slot, version_id: VersionId, request: Request, store=Depends(site_store)):
        guard_mirror(request)
        store.delete_version(version_id, slot)
        return {'ok': True}

    @routes.post('/versions/{slot}/cleanup')
    def cleanup_slot_versions(slot: Slot, request: Request, store=Depends(site_store)):
        guard_mirror(request)
        return {'deleted': len(store.delete_unpublished_versions(slot))}

    def source_version(store, slot, version_id):
        version = store.version(version_id)
        if not version or version['slot'] != slot:
            raise HTTPException(404, '版本不存在或槽位不符')
        return version

    app.state.link_checker = LinkChecker()
    app.state.trust_checker = TrustChecker(device_from_environ())
    from .desk import DeskError, SheetWriter, buyer_quote, review_tickets
    from .desk_window import WorkOrderWindow
    app.state.desk_reader = WorkOrderWindow(Path(data_dir) / 'desk-browser', timeout=180)
    app.state.sheet_writer = SheetWriter(os.environ.get('AB_GOOGLE_SHEETS_TOKEN', ''))

    def redirect_commit_guard(request, store):
        site_id = request.path_params.get('site_id', 'default')
        expected_site = authorized_site(request, site_id)
        account_id = principal(request)['id']
        token = request.cookies.get(COOKIE)

        def guard(db):
            # Lock identity and ownership with the business commit. A check can
            # take seconds; transfers or revoked sessions must win immediately.
            db.execute('ATTACH DATABASE ? AS redirect_registry', (str(registry.path),))
            identity = 'main'
            if auth and accounts.store.path != store.path:
                db.execute('ATTACH DATABASE ? AS redirect_identity', (str(accounts.store.path),))
                identity = 'redirect_identity'
            db.execute('BEGIN IMMEDIATE')
            if auth:
                session = db.execute(f'''SELECT a.id,a.role FROM {identity}.account_sessions s JOIN {identity}.accounts a
                    ON a.id=s.account_id WHERE s.token_hash=? AND s.expires>? AND a.enabled=1''', (auth.key(token or ''), time.time())).fetchone()
                if not session or session['id'] != account_id:
                    raise HTTPException(401, '登录已失效，请重新登录')
                role = session['role']
            else:
                role = 'admin'
            current = db.execute('SELECT owner_id,owner_epoch FROM redirect_registry.sites WHERE id=?', (site_id,)).fetchone()
            if not current or current['owner_epoch'] != expected_site['owner_epoch'] or (role != 'admin' and current['owner_id'] != account_id):
                raise HTTPException(404, '站点归属已改变，请刷新域名列表')
        return guard

    @routes.get('/b-redirects')
    def get_b_redirects(version_id: str | None = Query(default=None, pattern=r'^[a-f0-9]{32}$'), store=Depends(site_store)):
        published = store.slots()['B']
        version_id = version_id or published
        base, occurrences, warnings = None, [], []
        contact = {'has_config': False, 'readable': True, 'contact_app': {'present': False, 'value': ''}, 'band_url': {'present': False, 'value': ''}, 'kakao_url': {'present': False, 'value': ''}}
        implant = {'state': 'absent', 'label': '还没有 B 页'}
        if version_id:
            base = source_version(store, 'B', version_id)
            occurrences, warnings = scan_bundle(store.pages / version_id, version_id)
            contact = scan_contact(store.pages / version_id)
            implant = scan_implant(store.pages / version_id)
        return {'version':base, 'published_version':published, 'occurrences':public_occurrences(occurrences),
                'warnings':warnings, 'presets':store.redirect_presets(), 'numbers':store.whatsapp_numbers(),
                'trust_poll':store.whatsapp_trust_poll_enabled(), 'links':store.contact_links(), 'contact':contact,
                'implant':implant,
                'active':store.redirect_active(), 'split':store.redirect_split(), 'number_split':store.whatsapp_split()}

    @routes.put('/b-redirects/split')
    def save_b_redirect_split(body: RedirectSplit, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        result = {'split': store.save_redirect_split(body.enabled, body.mode, body.members)}
        # The link-split picker is added when the page is served, so drop the
        # cached copy or visitors keep the previous split for up to the edge TTL.
        purged = after_site_change(site_id)
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.post('/b-redirects/presets')
    def add_b_redirect_presets(body: RedirectPresetInput, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        saved = store.add_redirect_presets(body.urls, body.note)
        sync_followers(site_id)
        return {'presets': saved}

    @routes.delete('/b-redirects/presets/{preset_id}')
    def delete_b_redirect_preset(preset_id: int, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        store.delete_redirect_preset(preset_id)
        sync_followers(site_id)
        return {'ok':True}

    @routes.post('/b-redirects/presets/{preset_id}/check')
    def check_b_redirect_preset(preset_id: int, request: Request, store=Depends(site_store)):
        guard = redirect_commit_guard(request, store)
        preset = store.redirect_preset(preset_id)
        result = app.state.link_checker.check(preset['url'])
        return {'preset':store.save_redirect_check(preset, result, guard)}

    @routes.post('/b-redirects/apply')
    def apply_b_redirects(body: RedirectApply, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        guard = redirect_commit_guard(request, store)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换链')
        occurrences, _ = scan_bundle(store.pages / base['id'], base['id'])
        if body.occurrence_ids:
            chosen = [item for item in occurrences if item['id'] in set(body.occurrence_ids)]
            if len(chosen) != len(body.occurrence_ids):
                raise Conflict('源码或跳转位置已变化，请重新扫描后再换链')
            if any(item['kind'] == 'whatsapp_number' for item in chosen):
                raise ValueError('WhatsApp 号码请用号码预设更换')
        elif not has_whatsapp_builder(store.pages / base['id']):
            raise ValueError('这一版没有 WhatsApp 跳转函数，没有改点击')
        preset = store.redirect_preset(body.preset_id)
        result = app.state.link_checker.check(preset['url'])
        store.save_redirect_check(preset, result, guard)
        if result['status'] == 'abnormal':
            raise ValueError('此链接不正常：' + result['detail'])
        version, changed = store.apply_redirects(base, preset, body.occurrence_ids, body.expected_published, guard)
        response = {'version': version, 'check': result, 'changed': changed}
        if changed:
            purged = after_site_change(site_id)
            if purged is not None:
                response['cloudflare'] = purged
        return response

    @routes.post('/b-redirects/numbers')
    def add_whatsapp_numbers(body: WhatsAppNumberInput, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        saved = store.add_whatsapp_numbers(body.phones, body.note, body.display_name)
        sync_followers(site_id)
        return {'numbers': saved}

    @routes.post('/b-redirects/numbers/reception')
    def publish_whatsapp_reception(body: WhatsAppReception, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        guard = redirect_commit_guard(request, store)
        number = store.upsert_whatsapp_name(body.phone, body.display_name)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换号')
        version, changed = store.publish_reception(base, body.display_name, body.expected_published, guard)
        response = {'number': number, 'version': version, 'changed': changed, 'sentence': f'本次由助理{body.display_name} 接待'}
        if changed:
            purged = after_site_change(site_id)
            if purged is not None:
                response['cloudflare'] = purged
        else:
            sync_followers(site_id)
        return response

    @routes.post('/b-redirects/direct-entry')
    def publish_direct_entry(body: DirectEntry, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        guard = redirect_commit_guard(request, store)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换号')
        version, changed, kind = store.publish_direct_entry(base, body.expected_published, guard)
        purged = after_site_change(site_id) if changed else None
        return {'version': version, 'changed': changed, 'kind': kind, 'cloudflare': purged}

    @routes.put('/b-redirects/numbers/split')
    def save_whatsapp_number_split(body: WhatsAppSplit, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        saved = store.save_whatsapp_split(body.enabled, body.mode, body.members)
        result = {'number_split': saved}
        # The landing stays cached for the speed rule. Drop that copy so the
        # shared picker script lists the pool that was just saved.
        purged = after_site_change(site_id)
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.delete('/b-redirects/numbers/{number_id}')
    def delete_whatsapp_number(number_id: int, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        store.delete_whatsapp_number(number_id)
        result = {'ok': True}
        # A deleted number drops out of the served split picker, so refresh the
        # cached page when a split is live.
        if store.whatsapp_split()['enabled']:
            purged = after_site_change(site_id)
            if purged is not None:
                result['cloudflare'] = purged
        else:
            sync_followers(site_id)
        return result

    @routes.post('/wa-trust/screen/{token}')
    async def ingest_whatsapp_screen(token: str, request: Request):
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', token):
            raise HTTPException(404, '检测已结束')
        raw = await request.body()
        if not app.state.trust_checker.deliver(token, raw.decode('utf-8', 'replace')):
            raise HTTPException(404, '检测已结束')
        return {'ok': True}

    @routes.post('/b-redirects/numbers/{number_id}/trust')
    def check_whatsapp_trust(number_id: int, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        number = store.whatsapp_number(number_id)

        def persist(outcome):
            saved = store.save_whatsapp_trust(number_id, outcome['status'], outcome.get('reason') or '')
            sync_followers(site_id)
            return saved

        result = app.state.trust_checker.check(number['phone'], persist)
        if result.get('busy'):
            return {'number':store.whatsapp_number(number_id), 'busy':True, 'poll_enabled':store.whatsapp_trust_poll_enabled()}
        return {'number':result['number'], 'busy':False, 'poll_enabled':store.whatsapp_trust_poll_enabled()}

    @routes.post('/desk/review')
    async def review_desk(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, '工单数据无法读取')
        try:
            return await asyncio.to_thread(review_tickets, body.get('tickets'), str(body.get('phone') or ''), app.state.desk_reader)
        except DeskError as error:
            raise HTTPException(400, str(error)) from None

    @routes.get('/desk/screen')
    def desk_screen():
        snap = getattr(app.state.desk_reader, 'snapshot', None)
        frame = snap() if snap else b''
        if not frame:
            return Response(status_code=204)
        return Response(frame, media_type='image/png', headers={'Cache-Control': 'no-store'})

    @routes.post('/desk/screen/click')
    async def desk_screen_click(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, '点击位置不对')
        x, y = body.get('x'), body.get('y')
        if isinstance(x, bool) or isinstance(y, bool):
            raise HTTPException(400, '点击位置不对')
        try:
            point = (float(x), float(y))
        except (TypeError, ValueError):
            raise HTTPException(400, '点击位置不对') from None
        pointer = getattr(app.state.desk_reader, 'pointer', None)
        if pointer is None:
            raise HTTPException(400, '当前没有窗口')
        try:
            pointer(point[0], point[1])
        except DeskError as error:
            raise HTTPException(400, str(error)) from None
        return {'ok': True}

    @routes.post('/desk/quote')
    async def quote_desk(request: Request):
        body = await request.json()
        try:
            return buyer_quote(body)
        except DeskError as error:
            raise HTTPException(400, str(error)) from None

    @routes.post('/desk/sheet')
    async def write_desk_sheet(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, '金额内容无法读取')
        try:
            quote = buyer_quote(body)
            result = app.state.sheet_writer.append(str(body.get('spreadsheet') or ''), quote['row'])
        except DeskError as error:
            raise HTTPException(400, str(error)) from None
        return {'ok': True, 'text': quote['text'], 'row': quote['row'], 'updated': result.get('updated', 1)}

    @routes.post('/b-redirects/numbers/apply')
    def apply_whatsapp_numbers(body: WhatsAppNumberApply, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        guard = redirect_commit_guard(request, store)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换号')
        occurrences, _ = scan_bundle(store.pages / base['id'], base['id'])
        chosen = [item for item in occurrences if item['id'] in set(body.occurrence_ids)]
        if len(chosen) != len(body.occurrence_ids):
            raise Conflict('源码或跳转位置已变化，请重新扫描后再换号')
        if any(item['kind'] != 'whatsapp_number' for item in chosen):
            raise ValueError('号码预设只能更换 WhatsApp 号码')
        number = store.whatsapp_number(body.number_id)
        version, changed = store.apply_whatsapp_number(base, number, body.occurrence_ids, body.expected_published, guard)
        purged = after_site_change(site_id) if changed else None
        return {'version':version, 'changed':changed, 'cloudflare':purged}

    @routes.post('/b-redirects/links')
    def add_contact_links(body: ContactLinkInput, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        saved = store.add_contact_links(body.app, body.urls, body.note)
        sync_followers(site_id)
        return {'links': saved}

    @routes.delete('/b-redirects/links/{link_id}')
    def delete_contact_link(link_id: int, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        store.delete_contact_link(link_id)
        sync_followers(site_id)
        return {'ok': True}

    @routes.post('/b-redirects/links/apply')
    def apply_contact_links(body: ContactLinkApply, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        guard = redirect_commit_guard(request, store)
        link = store.contact_link(body.link_id)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换')
        version, changed, summary = store.apply_contact_link(base, link, body.expected_published, guard)
        purged = after_site_change(site_id) if changed else None
        return {'version': version, 'changed': changed, 'contact_app': summary['contact_app'], 'url': summary['url'], 'contact_value': summary['contact_value'], 'cloudflare': purged}

    @routes.get('/tracking')
    def get_tracking(store=Depends(site_store)):
        return {'snippets': registry.tracking_snippets(), 'published': store.published_tracking()}

    @routes.post('/tracking')
    def add_tracking(body: TrackingSnippetInput, store=Depends(site_store)):
        label, snippet = normalize_ga4(body.body) if body.kind == 'ga4' else normalize_conversion(body.body)
        return {'snippet': registry.add_tracking_snippet(body.kind, label, snippet, body.note)}

    @routes.delete('/tracking/{snippet_id}')
    def delete_tracking(snippet_id: int, store=Depends(site_store)):
        registry.delete_tracking_snippet(snippet_id)
        return {'ok': True}

    @routes.post('/tracking/apply')
    def apply_tracking(body: TrackingApply, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        if body.ga4_id is None and body.conversion_id is None:
            raise ValueError('请选择要写入的 GA4 或转化代码')
        ga4 = registry.tracking_snippet(body.ga4_id) if body.ga4_id else None
        conversion = registry.tracking_snippet(body.conversion_id) if body.conversion_id else None
        if ga4 and ga4['kind'] != 'ga4':
            raise ValueError('选中的不是 GA4 代码')
        if conversion and conversion['kind'] != 'conversion':
            raise ValueError('选中的不是转化代码')
        version = store.apply_tracking(body.slot, ga4['body'] if ga4 else None, conversion['body'] if conversion else None, body.expected_published)
        purged = after_site_change(site_id)
        return {'version': version, 'published': store.published_tracking(), 'cloudflare': purged}

    @routes.get('/page-title')
    def get_page_title(store=Depends(site_store)):
        return {'titles': store.published_titles()}

    @routes.post('/page-title')
    def set_page_title(body: PageTitleUpdate, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        version, title = store.apply_page_title(body.slot, body.title, body.expected_published)
        result = {'version': version, 'title': title, 'titles': store.published_titles()}
        purged = after_site_change(site_id)
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.get('/source/{slot}/{version_id}')
    def get_source(slot: Slot, version_id: VersionId, path: str | None = None, store=Depends(site_store)):
        source_version(store, slot, version_id)
        try:
            files = source_files(store.pages / version_id)
            if path is not None:
                data = {'path': path, 'content': read_source(editable_file(files, path), path).decode('utf-8-sig')}
            else:
                data = {'files': [{'path': name, 'bytes': size} for name, (_, size) in sorted(files.items()) if Path(name).suffix.lower() in EDITABLE_EXTENSIONS]}
        except FileNotFoundError:
            raise HTTPException(404, '源码文件不存在') from None
        return data | {'published_version': store.slots()[slot]}

    @routes.post('/source/{slot}/{version_id}')
    def save_source(slot: Slot, version_id: VersionId, body: SourceEdit, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        base = source_version(store, slot, version_id)
        try:
            version = store.save_source(slot, base, body.path, body.content, body.expected_published)
        except FileNotFoundError:
            raise HTTPException(404, '源码文件不存在') from None
        result = {'version': version, 'published_version': version['id']}
        purged = after_site_change(site_id)
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.post('/preview')
    def preview(body: VersionChoice, request: Request, store=Depends(site_store)):
        if not store.version(body.version_id):
            raise HTTPException(404, '版本不存在')
        expiry = int(time.time()) + 300
        site_id = request.path_params.get('site_id', 'default')
        issuer = principal(request)['id'] if auth else 'local'
        signature = preview_signature(store, accounts.store, registry.get(site_id), expiry, body.version_id, issuer, not deployment)
        if signature is None:
            raise HTTPException(404, '预览不存在或已过期')
        prefix = '' if site_id == 'default' else f'/_sites/{site_id}'
        return {'url': f'{target_url}{prefix}/_preview/{expiry}/{body.version_id}/{issuer}/{signature}/index.html'}

    @routes.post('/links')
    def add_links(body: LinkInput, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        store.add_links(body.slot, body.urls)
        sync_followers(site_id)
        return {'ok': True}

    @routes.delete('/links/{link_id}')
    def delete_link(link_id: int, request: Request, store=Depends(site_store)):
        site_id = guard_mirror(request)
        store.delete_link(link_id)
        sync_followers(site_id)
        return {'ok': True}

    @routes.post('/counters/reset')
    def reset_counts(request: Request, store=Depends(site_store)):
        guard_mirror(request)
        store.reset_counts()
        return {'ok': True}

    @routes.post('/simulate')
    def simulate(body: Visitor, store=Depends(site_store)):
        config, _ = store.config()
        return decide(config, body)

    def viewed_records(request):
        site = registry.get(request.path_params.get('site_id', 'default'))
        reading, mirror = records_of(site)
        return site, reading, mirror

    def present_record_rows(site, rows, mirror):
        if not mirror:
            return rows
        source_name = source_domain_of(site)
        label = site.get('domain') or '本地站点'
        for row in rows:
            relabel_row(row, source_name, label)
        return rows

    @routes.get('/logs')
    def logs(request: Request, days: int = Query(default=7, ge=1, le=30), slot: Literal['', 'A', 'B'] = '', page: int = Query(default=1, ge=1, le=10000), store=Depends(site_store)):
        site, reading, mirror = viewed_records(request)
        data = _present_logs(reading.logs(days, slot, page))
        present_record_rows(site, data['items'], mirror)
        return data

    @routes.get('/access-report')
    def access_report(request: Request, days: int = Query(default=7, ge=1, le=90), country: str = '', store=Depends(site_store)):
        code = country.strip().upper()
        if code and not re.fullmatch(r'[A-Z]{2}', code):
            raise HTTPException(400, '国家代码应为两位字母')
        site, reading, mirror = viewed_records(request)
        label = site.get('domain') or '本地站点'
        text = reading.access_report(days, code or None, label, rename_from=source_domain_of(site) if mirror else '')
        return Response(text, media_type='text/plain; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="access-report.txt"'})

    @routes.get('/logs/rate')
    def log_rate(request: Request, store=Depends(site_store)):
        _site, reading, _mirror = viewed_records(request)
        return {'count': reading.recent_count(60), 'seconds': 60}

    @routes.post('/logs/clear')
    def clear_logs(request: Request, store=Depends(site_store)):
        site = registry.get(request.path_params.get('site_id', 'default'))
        if site.get('mirror_source'):
            raise HTTPException(400, '观察域名只读取投放域名的记录，不能在这里清空')
        return {'deleted': store.clear_logs()}

    @routes.post('/logs/clear-foreign')
    def clear_foreign_logs(request: Request, store=Depends(site_store)):
        site = registry.get(request.path_params.get('site_id', 'default'))
        if site.get('mirror_source'):
            raise HTTPException(400, '观察域名只读取投放域名的记录，不能在这里清空')
        return store.clear_logs_unless(displayed_hong_kong)

    @routes.get('/logs.csv')
    def export_logs(request: Request, days: int = Query(default=7, ge=1, le=30), slot: Literal['', 'A', 'B'] = '', store=Depends(site_store)):
        site, reading, mirror = viewed_records(request)
        fields = ['id', 'created', 'country', 'ip', 'device', 'device_name', 'device_model', 'os', 'browser', 'slot', 'reason', 'path', 'mode']
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(fields)
        rows = _present_logs(reading.logs(days, slot, page_size=10000))['items']
        present_record_rows(site, rows, mirror)
        for row in rows:
            details = row.get('device_details') or {}
            if '/' in str(row.get('ip') or ''):
                row['ip'] = ''
            row.update(device_name=details.get('device', ''), device_model=details.get('model', ''), os=details.get('os', ''), browser=details.get('browser', ''))
            values = [str(row.get(key) or '') for key in fields]
            writer.writerow(["'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for value in values])
        return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="ab-lab-visits.csv"'})

    @routes.get('/audit')
    def audit(request: Request, store=Depends(site_store)):
        items = store.audit()
        if not sees_everything(principal(request)):
            site_actions = {'config_updated', 'content_imported', 'version_published',
                            'counters_reset', 'logs_cleared', 'logs_foreign_cleared', 'analysis_cleared', 'links_added', 'link_deleted', 'b_redirect_presets_added', 'b_redirect_preset_deleted', 'a_version_deleted', 'b_version_deleted', 'b_redirect_split_updated', 'whatsapp_numbers_added', 'whatsapp_number_deleted', 'whatsapp_trust_checked', 'whatsapp_split_updated', 'contact_links_added', 'contact_link_deleted'}
            items = [item for item in items if item['action'] in site_actions]
        return {'items': items}

    def analysis_sites(request, include_mirrors=False):
        account = principal(request)
        if account['role'] == 'observer':
            site = registry.site_for_observer(account['id'])
            return [site] if site else []
        owner = None if account['role'] == 'admin' else account['id']
        sites = registry.list(owner)
        if not include_mirrors:
            sites = [site for site in sites if not (site.get('mirror_source') or '')]
        return sites

    def analysis_domain(site):
        return site.get('domain') or '本地站点'

    def scoped_analysis_sites(request, domain=''):
        wanted = (domain or '').strip().lower()
        sites = analysis_sites(request, include_mirrors=bool(wanted))
        if not wanted:
            return sites
        return [site for site in sites if analysis_domain(site).lower() == wanted]

    def stamp_rows(site, rows, kind):
        mirror = bool((site.get('mirror_source') or '').strip())
        label = analysis_domain(site)
        source_name = source_domain_of(site) if mirror else ''
        for row in rows:
            if mirror:
                relabel_row(row, source_name, label)
                row['domain'] = label
            elif kind == 'event':
                row['domain'] = displayed_event_domain(registry, row, label)
            elif kind == 'intake':
                row['domain'] = displayed_intake_domain(registry, row, label)
            else:
                row['domain'] = label
        return rows

    @app.get('/api/analysis')
    def analysis(request: Request, days: int = Query(default=7, ge=1, le=90), domain: str = ''):
        """Hong Kong rows for one selected domain, or every domain this account can see."""
        since = time.time() - days * 86400
        sites = scoped_analysis_sites(request, domain)
        requests, events = [], []
        request_total = event_total = 0
        for site in sites:
            reading, _mirror = records_of(site)
            access_count, access_rows = reading.recent_access(since, 200)
            event_count, event_rows = reading.recent_client_events(since, 200)
            request_total += access_count
            event_total += event_count
            requests.extend(stamp_rows(site, access_rows, 'visit'))
            events.extend(stamp_rows(site, event_rows, 'event'))
        requests.sort(key=lambda row: row['created'], reverse=True)
        events.sort(key=lambda row: row['created'], reverse=True)
        return {
            'days': days,
            'domains': len(sites),
            'requests': {'total': request_total, 'items': requests[:200]},
            'events': {'total': event_total, 'items': events[:200]},
        }

    def clear_selected_domain(request, domain):
        wanted = domain.strip()
        if not wanted:
            raise HTTPException(400, '请先选择域名')
        sites = scoped_analysis_sites(request, wanted)
        if len(sites) != 1:
            raise HTTPException(404, '没有这个域名')
        if sites[0].get('mirror_source'):
            raise HTTPException(400, '观察域名只读取投放域名的记录，不能在这里清空')
        counts = registry.store(sites[0]['id']).clear_analysis()
        counts['domain'] = analysis_domain(sites[0])
        return counts

    @app.post('/api/analysis/clear')
    def analysis_clear(request: Request, domain: str = ''):
        return clear_selected_domain(request, domain)

    @app.post('/api/campaign/clear')
    def campaign_clear(request: Request, domain: str = ''):
        return clear_selected_domain(request, domain)

    @app.get('/api/analysis/report')
    def analysis_report(request: Request, days: int = Query(default=7, ge=1, le=90), domain: str = ''):
        parts = []
        for site in scoped_analysis_sites(request, domain):
            reading, mirror = records_of(site)
            text = reading.access_report(days, 'HK', analysis_domain(site), rename_from=source_domain_of(site) if mirror else '')
            parts.append(f'===== {analysis_domain(site)} =====\n{text}')
        body = '\n'.join(parts) if parts else '没有可查看的域名。\n'
        return Response(body, media_type='text/plain; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="analysis-report.txt"'})

    def watched_sites(request, domain):
        account = principal(request)
        wanted = (domain or '').strip().lower()
        chosen = []
        for site in analysis_sites(request, include_mirrors=True):
            mirror = bool((site.get('mirror_source') or '').strip())
            if mirror and account['role'] != 'observer' and analysis_domain(site).lower() != wanted:
                continue
            chosen.append(site)
        return chosen

    def collect_campaign(request, since, until, domain=''):
        visits, events = [], []
        for site in watched_sites(request, domain):
            reading, _mirror = records_of(site)
            visits.extend(stamp_rows(site, reading.campaign_access(since, until), 'visit'))
            events.extend(stamp_rows(site, reading.campaign_events(since, until), 'event'))
        return visits, events

    def collect_intake(request, since, until, domain=''):
        rows = []
        for site in watched_sites(request, domain):
            reading, _mirror = records_of(site)
            rows.extend(stamp_rows(site, reading.intake_between(since, until), 'intake'))
        return rows

    def campaign_arguments(span, start, end, domain, placement, device, wa_env, link_type, model, placement_exact=False, dest_app='', youtube='', opener=''):
        since, until = window_bounds(span, start, end, time.time())
        filters = {
            'domain': domain.strip()[:253],
            'placement': (placement if placement_exact else placement.strip())[:300],
            'device': device.strip()[:80],
            'wa_env': wa_env.strip()[:80],
            'link_type': link_type.strip()[:80],
            'model': model.strip()[:80],
            'dest_app': dest_app.strip().lower()[:20],
            'youtube': youtube.strip()[:120],
            'opener': opener.strip()[:40],
            'placement_exact': bool(placement_exact),
        }
        return since, until, filters

    @app.get('/api/campaign')
    def campaign_table(request: Request, span: Literal['today', 'yesterday', '7d', 'custom'] = Query(default='today', alias='range'),
                       start: str = '', end: str = '', domain: str = '', placement: str = '', device: str = '',
                       wa_env: str = '', link_type: str = '', model: str = '', dest_app: str = '', youtube: str = '', opener: str = '', placement_exact: bool = False):
        since, until, filters = campaign_arguments(span, start, end, domain, placement, device, wa_env, link_type, model, placement_exact, dest_app, youtube, opener)
        visits, events = collect_campaign(request, since, until, filters['domain'])
        report = _with_youtube_channels(build_campaign(visits, events, filters))
        report['endpoint'] = jump_event_endpoint(deployment)
        report['range'] = span
        report['since'] = since
        report['until'] = until
        return report

    @app.get('/api/campaign.csv')
    def campaign_csv(request: Request, slot: Literal['today', 'yesterday', '7d', 'custom'] = Query(default='today', alias='range'),
                     start: str = '', end: str = '', domain: str = '', placement: str = '', device: str = '',
                     wa_env: str = '', link_type: str = '', model: str = '', dest_app: str = '', youtube: str = '', opener: str = '', placement_exact: bool = False):
        since, until, filters = campaign_arguments(slot, start, end, domain, placement, device, wa_env, link_type, model, placement_exact, dest_app, youtube, opener)
        visits, events = collect_campaign(request, since, until, filters['domain'])
        report = _with_youtube_channels(build_campaign(visits, events, filters))
        fields = ['时间', '域名', '去向', '版位', '频道', '打开', 'device', 'wa_env', 'link_type', '机型', '系统', '进站数', 'page_view', 'gate_open', 'gate_step2', 'gate_close', '点击人数', '点击次数', '拉起数', '兜底数', '点击率', '拉起率', '兜底点击', '复制号码', '复制消息', '标红', '原因']
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(fields)
        def cell(value):
            text = '' if value is None else str(value)
            return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
        def percent(value):
            return '' if value is None else f'{value * 100:.1f}%'
        def clock(stamp):
            if not stamp:
                return ''
            return datetime.fromtimestamp(float(stamp), timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        for row in report['rows']:
            writer.writerow([cell(value) for value in (
                clock(row.get('seen_at')), row['domain'], row['dest_label'], row['placement_label'], row['youtube_label'], row.get('opener') or '', row['device'], row['wa_env'], row['link_type'], row['model'], row['os'],
                row['visits'], row.get('page_view') or 0, row.get('gate_open') or 0, row.get('gate_step2') or 0, row.get('gate_close') or 0, row['clicks'], row.get('click_times') or 0, row['opens'], row['fallbacks'], percent(row['click_rate']), percent(row['open_rate']),
                row['fallback_click'], row['copy_number'], row['copy_message'], '是' if row['alert'] else '', row['alert_reason'],
            )])
        return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="campaign.csv"'})

    @app.get('/api/campaign/events')
    def campaign_events_export(request: Request, span: Literal['today', 'yesterday', '7d', 'custom'] = Query(default='today', alias='range'),
                               start: str = '', end: str = '', domain: str = '', placement: str = '', device: str = '',
                               wa_env: str = '', link_type: str = '', model: str = '', dest_app: str = '', youtube: str = '', opener: str = '', placement_exact: bool = False):
        since, until, filters = campaign_arguments(span, start, end, domain, placement, device, wa_env, link_type, model, placement_exact, dest_app, youtube, opener)
        _visits, events = collect_campaign(request, since, until, filters['domain'])
        payload = json.dumps(export_events(events, filters), ensure_ascii=False, separators=(',', ':'))
        return Response(payload, media_type='application/json; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="campaign-events.json"'})

    def flow_window(span, start, end):
        try:
            return window_bounds(span, start, end, time.time())
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    def flow_report(request, span, start, end, domain):
        since, until = flow_window(span, start, end)
        visits, events = collect_campaign(request, since, until, domain)
        return build_flow(visits, events, collect_intake(request, since, until, domain), domain)

    @app.get('/api/campaign/flow')
    def campaign_flow(request: Request, span: Literal['today', 'yesterday', '7d', 'custom'] = Query(default='today', alias='range'),
                      start: str = '', end: str = '', domain: str = ''):
        report = flow_report(request, span, start, end, domain)
        report['visits_total'] = len(report['visits'])
        report['visits'] = report['visits'][:200]
        since, until = flow_window(span, start, end)
        intake = collect_intake(request, since, until, domain)
        if domain.strip():
            intake = [row for row in intake if (row.get('domain') or '').lower() == domain.strip().lower()]
        report['intake_total'] = len(intake)
        report['intake'] = list(reversed(intake[-100:]))
        return report

    def flow_csv(text, filename):
        return Response('\ufeff' + text, media_type='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    @app.get('/api/campaign/flow.csv')
    def campaign_flow_csv(request: Request, span: Literal['today', 'yesterday', '7d', 'custom'] = Query(default='today', alias='range'),
                          start: str = '', end: str = '', domain: str = ''):
        return flow_csv(summary_csv(flow_report(request, span, start, end, domain)), 'flow-summary.csv')

    @app.get('/api/campaign/visits.csv')
    def campaign_visits_csv(request: Request, span: Literal['today', 'yesterday', '7d', 'custom'] = Query(default='today', alias='range'),
                            start: str = '', end: str = '', domain: str = ''):
        return flow_csv(visits_csv(flow_report(request, span, start, end, domain)), 'flow-visits.csv')

    @app.get('/api/campaign/intake.csv')
    def campaign_intake_csv(request: Request, span: Literal['today', 'yesterday', '7d', 'custom'] = Query(default='today', alias='range'),
                            start: str = '', end: str = '', domain: str = ''):
        since, until = flow_window(span, start, end)
        rows = collect_intake(request, since, until, domain)
        if domain.strip():
            rows = [row for row in rows if (row.get('domain') or '').lower() == domain.strip().lower()]
        return flow_csv(intake_csv(rows), 'flow-intake.csv')

    @app.get('/api/campaign/page')
    def campaign_page(request: Request, domain: str = ''):
        wanted = domain.strip()
        if not wanted:
            raise HTTPException(400, '请先选择域名')
        sites = scoped_analysis_sites(request, wanted)
        if len(sites) != 1:
            raise HTTPException(404, '没有这个域名')
        try:
            payload, slot = served_landing_bytes(registry.store(sites[0]['id']), jump_event_endpoint(deployment))
        except FileNotFoundError:
            raise HTTPException(404, '当前发布页没有可导出的 HTML') from None
        return Response(payload, media_type='text/html; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="landing-{slot}.html"'})

    @app.post('/api/speedcheck')
    def start_speedcheck(body: SpeedCheckInput, request: Request):
        try:
            return app.state.speedcheck.start(body, principal(request)['id'])
        except SpeedBusy as error:
            raise HTTPException(409, str(error)) from None

    @app.get('/api/speedcheck/{job_id}')
    def read_speedcheck(job_id: Annotated[str, PathParameter(pattern=r'^[a-f0-9]{24}$')], request: Request):
        job = app.state.speedcheck.get(job_id, principal(request)['id'])
        if job is None:
            raise HTTPException(404, '检测不存在')
        return job

    @app.post('/wa-events')
    async def wa_events(request: Request):
        return await accept_jump_event(request, registry, store)

    app.include_router(routes, prefix='/api/sites/{site_id}')
    app.include_router(routes, prefix='/api')
    return app


def directory_redirect(store, version_ids, path, request):
    """Canonicalize before counting; the final directory document counts once."""
    if not path or path.endswith('/'):
        return None
    for version_id in version_ids:
        if not version_id:
            continue
        root = store.pages / version_id
        try:
            resolved = resolve_file(root, path)
        except (ValueError, FileNotFoundError):
            continue
        if (root / path).is_dir() and resolved.name == 'index.html':
            url = quote(getattr(request.state, 'public_prefix', '') + request.url.path + '/', safe='/')
            if request.url.query:
                url += '?' + request.url.query
            return RedirectResponse(url, status_code=307)
    return None


def cached_split_scan(root, version_id):
    with _split_scan_lock:
        cached = _split_scans.get(version_id)
    if cached is not None:
        return cached
    try:
        occurrences, _ = scan_bundle(root, version_id)
    except ValueError:
        occurrences = []
    with _split_scan_lock:
        if len(_split_scans) > 16:
            _split_scans.clear()
        _split_scans[version_id] = occurrences
    return occurrences


def assign_visitor_page(data, relative, occurrences, link_url, number):
    """One pass so link offsets and number offsets stay on the original file."""
    positions = [item for item in occurrences if item['path'] == relative]
    chosen = []
    for item in positions:
        if number and item['kind'] == 'whatsapp_number':
            chosen.append(dict(item, _assign=number['phone']))
        elif link_url and item['kind'] != 'whatsapp_number':
            chosen.append(dict(item, _assign=link_url))
    if chosen:
        data = rewrite_text(data, chosen, link_url or number['phone'])
    if number and number.get('display_name'):
        data = rewrite_reception_bytes(data, number['display_name'])
    return data


_NUMBER_SPLIT_SCRIPT = (
    '<script id="ab-number-split">(function(){'
    'var spec=__SPEC__;'
    'var pool=spec.pool||[];'
    'if(!pool.length)return;'
    'var key="ab_wa_pick",saved="",pick=null,i;'
    'try{saved=localStorage.getItem(key)||"";}catch(e){}'
    'for(i=0;i<pool.length;i++){if(pool[i].phone===saved){pick=pool[i];break;}}'
    'if(!pick){'
    'if(spec.mode==="weighted"){'
    'var total=0,j,draw,covered=0;'
    'for(j=0;j<pool.length;j++)total+=pool[j].weight||0;'
    'if(total>0){draw=Math.floor(Math.random()*total);'
    'for(j=0;j<pool.length;j++){covered+=pool[j].weight||0;if(draw<covered){pick=pool[j];break;}}}'
    '}'
    'if(!pick)pick=pool[Math.floor(Math.random()*pool.length)];'
    'try{localStorage.setItem(key,pick.phone);}catch(e){}'
    '}'
    'var phone=String(pick.phone||"").replace(/\\D/g,"");'
    'var name=pick.name||"";'
    'try{if(typeof CONFIG==="object"&&CONFIG){CONFIG.eventEndpoint=__ENDPOINT__;CONFIG.whatsappNumber=phone;if(name)CONFIG.receptionist=name;}}catch(e){}'
    'var links=document.querySelectorAll("a[href]");'
    'for(var n=0;n<links.length;n++){'
    'var href=links[n].getAttribute("href")||"";'
    'var next=href.replace(/wa\\.me\\/\\d{8,15}/g,"wa.me/"+phone).replace(/([?&]phone=)\\d{8,15}/g,"$1"+phone);'
    'if(next!==href)links[n].setAttribute("href",next);'
    '}'
    'if(name){var node=document.getElementById("reception-text");'
    'if(node)node.textContent="\\u672c\\u6b21\\u7531\\u52a9\\u7406"+name+" \\u63a5\\u5f85";}'
    '})();</script>'
)

# Runs after the page's own functions. Band and Kakao replace the click. A jumpUrl
# does the same for a group link while WhatsApp event names stay put.
# YouTube fields are copied from the address bar and the referrer; event names stay until the link changes.
_DEST_APP_SCRIPT = (
    '<script id="ab-dest-app">(function(){'
    'function ytFrom(raw){'
    'var text=String(raw||"").replace(/^\\s+|\\s+$/g,"");'
    'if(!text)return null;'
    'var decoded=text;try{decoded=decodeURIComponent(text);}catch(e){}'
    'var match=decoded.match(/^(?:https?:\\/\\/)?(?:www\\.|m\\.)?(youtube\\.com|youtu\\.be)(\\/[^\\s#]*)?/i);'
    'if(!match)return null;'
    'var host=match[1].toLowerCase(),path=match[2]||"/",query="",cut=path.indexOf("?");'
    'if(cut>=0){query=path.slice(cut+1);path=path.slice(0,cut);}'
    'var channel="",video="",found;'
    'found=path.match(/^\\/@([^\\/]+)/);if(found)channel="@"+found[1];'
    'else{found=path.match(/^\\/channel\\/([^\\/]+)/);if(found)channel=found[1];'
    'else{found=path.match(/^\\/(?:c|user)\\/([^\\/]+)/);if(found)channel=found[1];}}'
    'if(host==="youtu.be")video=(path.split("/")[1]||"");'
    'else{found=path.match(/^\\/(?:shorts|embed|live|v)\\/([^\\/]+)/);if(found)video=found[1];'
    'else if(path==="/watch"||path.indexOf("/watch/")===0){found=query.match(/(?:^|&)v=([^&]+)/);if(found)video=found[1];}}'
    'return{channel:channel.slice(0,120),video:video.slice(0,32),url:decoded.slice(0,300)};'
    '}'
    'function youtubeExtra(){'
    'var placement="",network="",referrer="",fromPlace=null,fromRef=null,net="";'
    'try{var params=new URLSearchParams(location.search);placement=params.get("placement")||"";network=params.get("network")||"";}catch(e){}'
    'try{referrer=document.referrer||"";}catch(e){}'
    'fromPlace=ytFrom(placement);fromRef=ytFrom(referrer);net=String(network||"").toLowerCase();'
    'if(!fromPlace&&!fromRef&&net!=="ytv"&&net!=="yts")return null;'
    'var out={},chosen=fromPlace||fromRef;'
    'if(net==="ytv"||net==="yts")out.network=net;'
    'if(chosen){if(chosen.channel)out.youtube_channel=chosen.channel;if(chosen.video)out.youtube_video=chosen.video;'
    'if(!fromPlace&&fromRef)out.youtube_referrer=chosen.url;}'
    'return out;'
    '}'
    'var cfg={},app="whatsapp",url="",switching=false,jumping=false;'
    'try{cfg=(typeof CONFIG==="object"&&CONFIG)||{};}catch(e){cfg={};}'
    'app=String(cfg.contactApp||"whatsapp").toLowerCase();'
    'if(app==="band"||app==="kakao"){'
    'url=String((app==="band"?(cfg.bandUrl||cfg.bandLink):(cfg.kakaoUrl||cfg.kakaoLink))||"").replace(/^\\s+|\\s+$/g,"");'
    'if(url)switching=true;}'
    'if(!switching){'
    'url=String(cfg.jumpUrl||"").replace(/^\\s+|\\s+$/g,"");'
    'if(/^https?:\\/\\//i.test(url))jumping=true;}'
    'if(switching||jumping){'
    'var urls=function(){var phone="";try{phone=String(cfg.whatsappNumber||"").replace(/\\D/g,"");}catch(e){}'
    'return{phone:phone,universal:url,scheme:url,intent:url};};'
    'try{buildWhatsAppUrls=urls;}catch(e){}'
    'try{window.buildWhatsAppUrls=urls;}catch(e){}'
    '}'
    'var previous=null;try{previous=sendEvent;}catch(e){}'
    'if(typeof previous!=="function")return;'
    'var map=switching?{whatsapp_click:app+"_click",wa_app_opened:app+"_app_opened",wa_fallback_shown:app+"_fallback_shown",wa_fallback_click:app+"_fallback_click",wa_copy_number:app+"_copy_number",wa_copy_message:app+"_copy_message"}:null;'
    'var wrapped=function(name,extra){'
    'var next=name,more=extra;'
    'try{more={};var key;if(extra&&typeof extra==="object"){for(key in extra){if(Object.prototype.hasOwnProperty.call(extra,key))more[key]=extra[key];}}'
    'if(switching)more.dest_app=app;'
    'var yt=youtubeExtra();if(yt){for(key in yt){if(Object.prototype.hasOwnProperty.call(yt,key))more[key]=yt[key];}}'
    'if(map&&map[name])next=map[name];}catch(err){next=name;more=extra;}'
    'return previous(next,more);};'
    'try{sendEvent=wrapped;}catch(e){}'
    'try{window.sendEvent=wrapped;}catch(e){}'
    '})();</script>'
)


def inject_dest_app(data, relative=''):
    """Serve-time wrapper. The stored page and its WhatsApp jump stay as uploaded."""
    if Path(str(relative)).suffix.lower() not in ('.html', '.htm'):
        return data
    if b'buildWhatsAppUrls' not in data or b'id="ab-dest-app"' in data:
        return data
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return data
    if 'id="ab-dest-app"' in text or 'buildWhatsAppUrls' not in text:
        return data
    match = None
    for found in re.finditer(r'</body\s*>', text, re.IGNORECASE):
        match = found
    index = match.start() if match else -1
    updated = text + _DEST_APP_SCRIPT if index < 0 else text[:index] + _DEST_APP_SCRIPT + text[index:]
    payload = updated.encode('utf-8')
    return (b'\xef\xbb\xbf' + payload) if bom else payload


_EVENT_ENDPOINT = re.compile(r'''(\beventEndpoint\b\s*[:=]\s*)(['"])([^'"\n]{0,200})\2''')


_youtube_names = {}
_youtube_lock = threading.Lock()


def lookup_youtube_channel(video_id):
    """Channel title for one video. A miss stays a video id; the click path never waits on this."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id or ''):
        return ''
    with _youtube_lock:
        cached = _youtube_names.get(video_id)
    if cached is not None:
        return cached
    name = ''
    try:
        from urllib.request import Request as UrlRequest, urlopen
        address = 'https://www.youtube.com/oembed?format=json&url=' + quote('https://www.youtube.com/watch?v=' + video_id, safe='')
        with urlopen(UrlRequest(address, headers={'User-Agent': 'Mozilla/5.0'}), timeout=2) as response:
            payload = json.loads(response.read(20000))
        if isinstance(payload, dict) and isinstance(payload.get('author_name'), str):
            name = payload['author_name'].strip()[:120]
    except Exception:
        name = ''
    with _youtube_lock:
        _youtube_names[video_id] = name
    return name


def _with_youtube_channels(report):
    fill_youtube_channels(report.get('rows') or [], lookup_youtube_channel)
    return report


def jump_event_endpoint(deployment):
    origin = deployment.admin_origin if deployment else PUBLIC_ORIGIN
    return origin.rstrip('/') + '/wa-events'


def fill_event_endpoint(data, url):
    """Write the receiver into an empty CONFIG.eventEndpoint. Leave every other byte alone."""
    if not url or b'eventEndpoint' not in data:
        return data
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return data

    def replace(match):
        if match.group(3).strip():
            return match.group(0)
        return f'{match.group(1)}{match.group(2)}{url}{match.group(2)}'

    updated = _EVENT_ENDPOINT.sub(replace, text)
    if updated == text:
        return data
    payload = updated.encode('utf-8')
    return (b'\xef\xbb\xbf' + payload) if bom else payload


def number_split_script(members, mode, event_endpoint=''):
    """One script for every visitor. The cached page can still give each phone its own jump."""
    pool = []
    for item in members:
        phone = re.sub(r'\D', '', str(item.get('phone') or ''))
        if not phone:
            continue
        try:
            weight = int(item.get('weight') or 1)
        except (TypeError, ValueError):
            weight = 1
        pool.append({'phone': phone, 'name': str(item.get('display_name') or ''), 'weight': weight if weight > 0 else 1})
    if not pool:
        return ''
    spec = json.dumps(
        {'mode': 'weighted' if mode == 'weighted' else 'random', 'pool': pool},
        ensure_ascii=False, separators=(',', ':'),
    )
    spec = spec.replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')
    spec = spec.replace('\u2028', r'\u2028').replace('\u2029', r'\u2029')
    endpoint = json.dumps(event_endpoint or jump_event_endpoint(None))
    return _NUMBER_SPLIT_SCRIPT.replace('__ENDPOINT__', endpoint).replace('__SPEC__', spec)


def inject_number_split(data, members, mode, event_endpoint=''):
    script = number_split_script(members, mode, event_endpoint)
    if not script:
        return data
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return data
    if 'id="ab-number-split"' in text:
        return data
    match = None
    for found in re.finditer(r'</body\s*>', text, re.IGNORECASE):
        match = found
    index = match.start() if match else -1
    updated = text + script if index < 0 else text[:index] + script + text[index:]
    payload = updated.encode('utf-8')
    return (b'\xef\xbb\xbf' + payload) if bom else payload


def _landing_html(root):
    direct = root / 'index.html'
    if direct.is_file():
        return direct, 'index.html'
    found = sorted(path for path in root.rglob('*') if path.is_file() and path.suffix.lower() in ('.html', '.htm'))
    if not found:
        raise FileNotFoundError('index.html')
    return found[0], found[0].resolve().relative_to(root.resolve()).as_posix()


def served_landing_bytes(store, endpoint):
    """The published page plus the scripts added at serve time. No visitor row is written."""
    config, _ = store.config()
    if config.routing == 'FORCE_A':
        slot = 'A'
    elif config.routing == 'FORCE_B':
        slot = 'B'
    else:
        slot = config.allowed_slot
    version_id = store.slots().get(slot) or ''
    if not version_id:
        raise FileNotFoundError(slot)
    root = store.pages / version_id
    file, relative = _landing_html(root)
    data = file.read_bytes()
    split = store.whatsapp_split()
    if split['enabled'] and split['members'] and Path(relative).suffix.lower() in ('.html', '.htm'):
        number = {'phone': split['members'][0]['phone'], 'display_name': split['members'][0].get('display_name') or ''}
        try:
            data = assign_visitor_page(data, relative, cached_split_scan(root, version_id), None, number)
        except ValueError:
            pass
        data = inject_number_split(data, split['members'], split['mode'], endpoint)
    return inject_dest_app(fill_event_endpoint(data, endpoint), relative), slot


def prepare_number_split(store, request, version_id):
    split = store.whatsapp_split()
    if not version_id or not split['enabled']:
        return None
    token = request.cookies.get(WA_COOKIE, '')
    is_new = not re.fullmatch(r'[a-f0-9]{64}', token)
    if is_new:
        token = secrets.token_hex(32)
    number = store.whatsapp_split_destination(hmac.new(store.secret, token.encode(), hashlib.sha256).hexdigest())
    if not number:
        return None
    return {
        'number': number,
        'occurrences': cached_split_scan(store.pages / version_id, version_id),
        'is_new': is_new,
        'token': token,
        'members': split['members'],
        'mode': split['mode'],
    }


def prepare_split(store, request, version_id):
    if not version_id or not store.redirect_split()['enabled']:
        return None
    token = request.cookies.get(SPLIT_COOKIE, '')
    is_new = not re.fullmatch(r'[a-f0-9]{64}', token)
    if is_new:
        token = secrets.token_hex(32)
    url = store.split_destination(hmac.new(store.secret, token.encode(), hashlib.sha256).hexdigest())
    if not url:
        return None
    occurrences = cached_split_scan(store.pages / version_id, version_id)

    def transform(data, relative):
        positions = [item for item in occurrences if item['path'] == relative and item['kind'] != 'whatsapp_number']
        if not positions:
            return data
        try:
            return rewrite_text(data, positions, url)
        except ValueError:
            return data

    return {'transform': transform, 'is_new': is_new, 'token': token, 'url': url}


def content_response(store, version_id, path, method='GET', transform=None):
    if not version_id:
        return HTMLResponse('<!doctype html><meta charset="utf-8"><h1>页面尚未发布</h1><p>请在管理后台为当前槽位导入并发布内容。</p>', status_code=503)
    try:
        root = store.pages / version_id
        file = resolve_file(root, path)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '资源不存在') from None
    mime = {'.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}.get(file.suffix.lower()) or mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
    if transform:
        data = transform(file.read_bytes(), file.resolve().relative_to(root.resolve()).as_posix())
        payload = b'' if method == 'HEAD' else data
        length = len(data)
    else:
        payload = file.read_bytes() if method != 'HEAD' else b''
        length = file.stat().st_size if method == 'HEAD' else len(payload)
    return Response(payload, media_type=mime, headers={'Content-Length': str(length)})


def _clip(value, limit):
    return (value or '')[:limit]


def _request_country(request, country):
    if getattr(request.state, 'access_country_ready', False):
        return getattr(request.state, 'access_country', country)
    ip = getattr(request.state, 'visitor_ip', request.client.host if request.client else '')
    return geo_country(ip) if ip else None


_access_jobs = queue.Queue(maxsize=2000)
_access_writer_started = False
_access_writer_lock = threading.Lock()


def _access_writer():
    while True:
        job = _access_jobs.get()
        try:
            job()
        except Exception:
            LOGGER.error('access log write failed')
        finally:
            _access_jobs.task_done()


def _ensure_access_writer():
    global _access_writer_started
    with _access_writer_lock:
        if not _access_writer_started:
            threading.Thread(target=_access_writer, name='access-log', daemon=True).start()
            _access_writer_started = True


def flush_access_log():
    """Wait until queued visitor rows are stored. Used by tests."""
    _access_jobs.join()


def _enqueue_access(job):
    _ensure_access_writer()
    try:
        _access_jobs.put_nowait(job)
    except queue.Full:
        LOGGER.error('access log queue full')


JUMP_EVENT_LIMIT = 8192
JUMP_HEADERS = {'Access-Control-Allow-Origin': '*'}


def jump_source_domain(request):
    """Landing domain from Origin, then Referer. Missing or opaque values stay blank."""
    for key in ('origin', 'referer'):
        raw = request.headers.get(key, '').strip()
        if not raw or raw.lower() == 'null':
            continue
        host = urlsplit(raw).hostname
        if host:
            return host[:253]
    return ''


def intake_reason(body, status):
    """Why this beacon was kept, rejected, or could not be read."""
    if status == 413:
        return '超过大小限制'
    if status == 403:
        return '来源、域名或代理校验失败'
    if not (body or '').strip():
        return '空 body'
    try:
        payload = json.loads(body)
    except ValueError:
        return '不是 JSON'
    if not isinstance(payload, dict):
        return 'JSON 不是对象'
    event = payload.get('event')
    if not isinstance(event, str) or not event.strip():
        return '缺少事件名'
    return '已接收'


def _beacon_store(registry, fallback_store, host):
    if host and registry is not None:
        site = registry.find_domain(host)
        if site is not None:
            return registry.store(site['id'])
    return fallback_store


def _queue_intake(store, ip, ua, origin, referer, content_type, body_length, body, status, reason):
    if store is None:
        return
    fields = ((ip or '')[:80], (ua or '')[:4096], (origin or '')[:500], (referer or '')[:2048], (content_type or '')[:200], int(body_length), (body or '')[:8192], int(status), (reason or '')[:80])
    _enqueue_access(lambda store=store, fields=fields: store.record_intake(*fields))


def _host_from_url(value):
    raw = (value or '').strip()
    if not raw or raw.lower() == 'null':
        return ''
    try:
        return (urlsplit(raw).hostname or '')[:253]
    except ValueError:
        return ''


def beacon_page_host(body):
    """Landing host carried inside the JSON when the browser sent no Origin."""
    if not body:
        return ''
    try:
        payload = json.loads(body)
    except ValueError:
        return ''
    if not isinstance(payload, dict):
        return ''
    page = payload.get('page')
    if not isinstance(page, str):
        return ''
    return _host_from_url(page)


def _known_domain(registry, host):
    if not host or registry is None:
        return ''
    site = registry.find_domain(host)
    if site is None:
        return ''
    return site.get('domain') or host


def registered_beacon_host(registry, body):
    """Page host only when it names a site we already track."""
    return _known_domain(registry, beacon_page_host(body))


def displayed_event_domain(registry, row, site_domain):
    """Keep a stored host. A blank host follows the page URL inside the body."""
    origin = (row.get('origin') or '').strip()
    if origin and origin.lower() != 'null':
        return origin
    return registered_beacon_host(registry, row.get('body') or '') or site_domain


def displayed_intake_domain(registry, row, site_domain):
    """Header host first. An opaque Origin still follows the page URL in the body."""
    header = _host_from_url(row.get('origin') or '') or _host_from_url(row.get('referer') or '')
    known = _known_domain(registry, header)
    if known:
        return known
    return registered_beacon_host(registry, row.get('body') or '') or site_domain


async def _read_asgi_body(receive, limit=8192):
    chunks = []
    size = 0
    while True:
        message = await receive()
        if message['type'] != 'http.request':
            break
        piece = message.get('body') or b''
        if size < limit:
            chunks.append(piece[:limit - size])
        size += len(piece)
        if not message.get('more_body') or size >= 1024 * 1024:
            break
    return b''.join(chunks), size


async def _record_rejected_beacon(registry, scope, receive):
    """A filtered beacon still arrived. Keep it, then return the same 403."""
    try:
        raw, size = await _read_asgi_body(receive)
        headers = {key.decode('latin1').lower(): value.decode('latin1', 'replace') for key, value in scope.get('headers') or []}
        text = raw.decode('utf-8', 'replace')
        host = _host_from_url(headers.get('origin') or headers.get('referer') or '') or registered_beacon_host(registry, text)
        store = _beacon_store(registry, registry.store('default') if registry is not None else None, host)
        peer = scope.get('client')
        ip = (scope.get('state') or {}).get('visitor_ip') or (peer[0] if peer else '')
        _queue_intake(store, ip, headers.get('user-agent', ''), headers.get('origin', ''), headers.get('referer', ''), headers.get('content-type', ''), size, text, 403, '来源、域名或代理校验失败')
    except Exception:
        LOGGER.error('rejected beacon log failed')


async def accept_jump_event(request, registry, fallback_store):
    """Store one beacon and answer before the write. Statistics must not slow the page."""
    raw = await request.body()
    status = 413 if len(raw) > JUMP_EVENT_LIMIT else 204
    body = raw[:8192].decode('utf-8', 'replace')
    reason = intake_reason(body if status == 204 else '', status)
    host = jump_source_domain(request) or registered_beacon_host(registry, body)
    store = _beacon_store(registry, fallback_store, host)
    ip = getattr(request.state, 'visitor_ip', '') or (request.client.host if request.client else '')
    _queue_intake(
        store, ip, request.headers.get('user-agent', ''), request.headers.get('origin', ''),
        request.headers.get('referer', ''), request.headers.get('content-type', ''), len(raw), body, status, reason,
    )
    if status == 204:
        _enqueue_access(lambda store=store, body=body, origin=host: store.record_client_event(body, origin))
    return Response(status_code=status, headers=JUMP_HEADERS)


def record_visit_access(store, request, status, started, wa_number, country):
    """Queue one Hong Kong visitor request. The page is not waiting on this write."""
    try:
        country = _request_country(request, country)
        if country != 'HK':
            return
        city = ''.join(ch for ch in unquote(request.headers.get('cf-ipcity', '')) if ch.isprintable())
        elapsed = max(0, int((time.perf_counter() - started) * 1000))
        fields = (
            _clip(request.headers.get('user-agent', ''), 2048),
            _clip(request.headers.get('sec-ch-ua', ''), 300),
            _clip(request.headers.get('sec-ch-ua-platform', ''), 80),
            _clip(request.headers.get('sec-ch-ua-platform-version', ''), 40),
            _clip(request.headers.get('sec-ch-ua-mobile', ''), 16),
            _clip(request.headers.get('referer', ''), 2048),
            _clip(request.url.query, 4096),
            _clip(wa_number, 32),
            _clip(request.headers.get('cf-cache-status', ''), 32),
            country,
            _clip(city, 80),
            int(status),
            elapsed,
            _clip(request.url.path, 512),
            _clip(getattr(request.state, 'visitor_ip', request.client.host if request.client else ''), 80),
        )
        _enqueue_access(lambda: store.record_access(*fields))
    except Exception:
        LOGGER.error('access log write failed')


def create_target(data_dir, port=8766, deployment=None, registry_dir=None):
    app, store = base_app(), Store(data_dir)
    account_store = store
    registry = Registry(data_dir, deployment.host(False) if deployment else '', registry_dir=registry_dir)
    app.add_middleware(LocalBoundary, port=port, deployment=deployment, registry=registry)

    def site_store(request: Request):
        return registry.store(request.state.site_id)

    @app.get('/.well-known/ab-lab-route/{site_id}')
    def route_proof(site_id: str, request: Request):
        if (not re.fullmatch('[a-f0-9]{32}', site_id)
                or request.state.site_id != site_id):
            raise HTTPException(404, '路由证明不存在')
        site = registry.get(site_id)
        return {'schema': 1, 'site_id': site_id, 'domain': site['domain']}

    @app.api_route('/_preview/{expiry}/{version_id}/{issuer}/{signature}/{path:path}', methods=['GET', 'HEAD'])
    def preview(expiry: int, version_id: str, issuer: str, signature: str, path: str, request: Request, store=Depends(site_store)):
        expected = preview_signature(store, account_store, registry.get(request.state.site_id), expiry, version_id, issuer, not deployment)
        if expiry < time.time() or expiry > time.time() + 301 or expected is None or not secrets.compare_digest(signature, expected) or not store.version(version_id):
            raise HTTPException(404, '预览不存在或已过期')
        redirect = directory_redirect(store, [version_id], path, request)
        if redirect is not None:
            return redirect
        return content_response(store, version_id, path, request.method, lambda data, relative: inject_dest_app(fill_event_endpoint(data, jump_event_endpoint(deployment)), relative))

    @app.post('/wa-events')
    async def wa_events(request: Request, store=Depends(site_store)):
        return await accept_jump_event(request, registry, store)

    @app.post('/api/collect')
    async def collect_client_event(request: Request, store=Depends(site_store)):
        # The landing page posts click events with sendBeacon. No origin check:
        # the page is sandboxed and the body is stored exactly as received.
        started = time.perf_counter()
        status = 204
        try:
            raw = await request.body()
            if len(raw) > 8192:
                raise HTTPException(413, '请求体超过大小限制')
            ip = getattr(request.state, 'visitor_ip', request.client.host if request.client else '')
            country = geo_country(ip) if ip else None
            request.state.access_country = country
            request.state.access_country_ready = True
            # Every site uses this same path. Only Hong Kong is kept, unchanged.
            if country == 'HK':
                text = raw.decode('utf-8', 'replace')
                _enqueue_access(lambda body=text: store.record_client_event(body))
            return Response(status_code=204)
        except HTTPException as error:
            status = error.status_code
            raise
        finally:
            record_visit_access(store, request, status, started, '', None)

    @app.api_route('/{path:path}', methods=['GET', 'HEAD'])
    def target(path: str, request: Request, store=Depends(site_store)):
        started = time.perf_counter()
        status = 500
        wa_number = ''
        country = None
        try:
            return _serve_target(path, request, store, lambda value: _remember(request, value))
        except HTTPException as error:
            status = error.status_code
            raise
        finally:
            record_visit_access(store, request, getattr(request.state, 'access_status', status), started, getattr(request.state, 'access_wa', wa_number), getattr(request.state, 'access_country', country))

    def _remember(request, value):
        request.state.access_status = value['status']
        request.state.access_wa = value['wa']
        request.state.access_country = value['country']
        request.state.access_country_ready = value['ready']

    def _serve_target(path, request, store, remember):
        def finish(response, wa='', seen_country=None, ready=False):
            remember({'status': response.status_code, 'wa': wa, 'country': seen_country, 'ready': ready})
            return response

        if path.startswith(('api/', 'data/', '_preview/', '_sites/')) or path in ('docs', 'openapi.json', 'redoc', '_sites'):
            raise HTTPException(404, '资源不存在')
        # ASGI already decodes the URL once. Do not unquote again.
        from .archives import safe_parts
        try:
            safe_parts(path.rstrip('/') or 'index.html')
        except ValueError:
            raise HTTPException(404, '资源不存在') from None
        config, _ = store.config()
        if config.content_mode == 'PAGE':
            redirect = directory_redirect(store, store.slots().values(), path, request)
            if redirect is not None:
                return finish(redirect)
        document = not path or path.endswith('/') or Path(path).suffix.lower() in ('.html', '.htm') or request.headers.get('sec-fetch-dest') == 'document'
        increment = document and request.method == 'GET'
        ip = getattr(request.state, 'visitor_ip', request.client.host)
        count = store.count(ip, config.rules.window_hours, increment)
        country = geo_country(ip)
        visitor = Visitor(ip=ip, country=country, ua=request.headers.get('user-agent', '')[:1024], language=request.headers.get('accept-language', '')[:512], visits=count)
        decision = decide(config, visitor)
        if increment:
            store.event(ip, country, decision, '/' + path, config.routing)
        slot = decision['slot']
        if config.content_mode == 'LINK':
            if not document:
                raise HTTPException(404, '资源不存在')
            link = store.choose_link(slot, config.distribution, consume=increment)
            if link:
                return finish(RedirectResponse(link['url'], status_code=302), seen_country=country, ready=True)
            return finish(HTMLResponse('<meta charset="utf-8"><h1>当前槽位尚未配置链接</h1>', status_code=503), seen_country=country, ready=True)
        version_id = store.slots()[slot]
        prepared = prepare_split(store, request, version_id) if slot == 'B' else None
        numbered = prepare_number_split(store, request, version_id) if slot == 'B' else None
        wa_number = (numbered['number'].get('phone') or '') if numbered and numbered.get('number') else ''
        endpoint = jump_event_endpoint(deployment)

        def visitor_transform(data, relative):
            if numbered:
                try:
                    data = assign_visitor_page(data, relative, numbered['occurrences'], prepared['url'] if prepared else None, numbered['number'])
                except ValueError:
                    pass
                if Path(relative).suffix.lower() in ('.html', '.htm'):
                    data = inject_number_split(data, numbered['members'], numbered['mode'], endpoint)
            elif prepared:
                data = prepared['transform'](data, relative)
            return inject_dest_app(fill_event_endpoint(data, endpoint), relative)

        transform = visitor_transform
        response = content_response(store, version_id, path, request.method, transform)
        if prepared and prepared['is_new']:
            response.set_cookie(SPLIT_COOKIE, prepared['token'], max_age=365 * 24 * 3600, secure=bool(deployment), httponly=True, samesite='lax', path='/')
        if numbered and numbered['is_new']:
            response.set_cookie(WA_COOKIE, numbered['token'], max_age=365 * 24 * 3600, secure=bool(deployment), httponly=True, samesite='lax', path='/')
        return finish(response, wa_number, country, True)

    return app
