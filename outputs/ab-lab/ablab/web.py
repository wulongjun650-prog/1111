import csv
import hashlib
import hmac
import io
import ipaddress
import logging
import mimetypes
import os
from pathlib import Path
import secrets
import time
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import maxminddb

from .archives import MAX_ZIP, import_content, resolve_file
from .models import ConfigUpdate, LinkInput, VersionChoice, Visitor
from .rules import decide
from .store import Conflict, Store
from .auth import Auth, COOKIE, SESSION_SECONDS
from .sites import Registry

ROOT = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger('ablab')
Slot = Literal['A', 'B']


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
        if self.deployment:
            allowed_hosts = (self.deployment.host(self.admin),)
            if not self.admin and self.registry:
                site = self.registry.for_host(host)
                allowed_hosts = (host,) if site else ()
            expected_origin = self.deployment.admin_origin
            try:
                state['visitor_ip'] = str(ipaddress.ip_address(headers.get(b'x-real-ip', b'').decode('ascii')))
            except (ValueError, UnicodeError):
                local = False
            local &= headers.get(b'x-forwarded-proto') == b'https'
        invalid = not local or host not in allowed_hosts
        if self.admin:
            invalid |= origin is not None and origin != expected_origin.encode()
            invalid |= headers.get(b'sec-fetch-site', b'none') not in (b'none', b'same-origin')
        if invalid:
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
            public = (scope['path'] == '/api/login' and scope['method'] == 'POST') or (scope['method'] in ('GET', 'HEAD') and scope['path'] in ('/login', '/static/login.js', '/static/app.css'))
            if not public and session is None:
                response = RedirectResponse('/login', status_code=307) if scope['path'] == '/' else JSONResponse({'detail': '请先登录'}, status_code=401)
                response.headers['Cache-Control'] = 'no-store'
                await response(scope, receive, send)
                return
            if session and not public:
                csrf = session['csrf']
                state['username'] = session['username']
        state['csrf'] = csrf
        if self.admin and scope['method'] not in ('GET', 'HEAD'):
            if origin != expected_origin.encode() or not secrets.compare_digest(headers.get(b'x-csrf-token', b''), csrf.encode()):
                await JSONResponse({'detail': '请求校验失败，请刷新页面'}, status_code=403)(scope, receive, send)
                return
        import re
        is_upload = re.fullmatch(r'/api/(?:sites/[a-f0-9]{32}/|sites/default/)?upload/[AB]', scope['path'])
        limit = MAX_ZIP if self.admin and is_upload else 256 * 1024
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
                    # Uploaded JS/forms are intentional; opaque origin prevents workers
                    # and storage persistence, and prevents access to the local admin.
                    ancestors = self.deployment.admin_origin if self.deployment else 'http://127.0.0.1:8765 http://localhost:8765'
                    forms = 'https:' if self.deployment else f'https: http://127.0.0.1:{self.port}'
                    policy = f"sandbox allow-scripts allow-forms; frame-ancestors {ancestors}; form-action {forms}; object-src 'none'"
                    extra.append((b'content-security-policy', policy.encode()))
                message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() not in {key for key, _ in extra}] + extra
            await send(message)
        await self.app(scope, replay, secured)


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


def create_admin(data_dir, port=8765, target_port=8766, deployment=None, registry_dir=None, server_ip=''):
    if server_ip:
        from .provisioning import public_ipv4
        server_ip = public_ipv4(server_ip)
    app, store = base_app(), Store(data_dir)
    app.state.csrf = secrets.token_urlsafe(32)
    app.state.store = store
    registry = Registry(data_dir, deployment.host(False) if deployment else '', [deployment.host(True)] if deployment else [], registry_dir)
    app.state.registry = registry
    auth = Auth(store) if deployment else None
    if auth and not auth.ready():
        raise ValueError('公网模式需要先在服务器终端设置管理员账号密码')
    target_url = deployment.target_origin if deployment else f'http://127.0.0.1:{target_port}'
    app.add_middleware(LocalBoundary, port=port, admin=True, csrf=app.state.csrf, target_port=target_port, deployment=deployment, auth=auth)
    templates = Jinja2Templates(directory=str(ROOT / 'templates'))
    app.mount('/static', StaticFiles(directory=str(ROOT / 'static'), check_dir=False), name='static')

    def site_store(request: Request):
        return registry.store(request.path_params.get('site_id', 'default'))

    def state(store, site_id):
        config, revision = store.config()
        site = registry.get(site_id)
        visit_url = target_url if site_id == 'default' else (f'https://{site["domain"]}' if deployment else f'{target_url}/_sites/{site_id}/')
        return {'config': config.model_dump(), 'revision': revision, 'versions': store.versions(), 'slots': store.slots(), 'links': store.links(), 'stats': store.stats(), 'health': {'geoip': geo_ready(), 'geoip_detail': geo_status(), 'local_only': deployment is None}, 'site': site, 'target_url': visit_url, 'preview_origin': target_url}

    @app.get('/api/sites')
    def list_sites():
        return {'sites': registry.list(), 'server_ip': server_ip, 'record_type': 'A', 'local_only': deployment is None}

    @app.post('/api/sites')
    def add_site(body: dict):
        if set(body) != {'domain'}:
            raise ValueError('只需提交域名')
        return {'site': registry.add(body['domain']), 'server_ip': server_ip, 'record_type': 'A'}

    @app.post('/api/sites/{site_id}/provision/{action}')
    def provision_control(site_id: str, action: Literal['pause', 'retry']):
        return {'site': registry.control(site_id, action)}

    @app.get('/api/sites/{site_id}/provision/events')
    def provision_events(site_id: str):
        return {'items': registry.events(site_id)}

    routes = APIRouter()

    @app.get('/', response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request=request, name='index.html', context={'csrf_token': request.state.csrf, 'target_url': target_url, 'deployment': bool(deployment), 'username': getattr(request.state, 'username', '')})

    if auth:
        @app.get('/login', response_class=HTMLResponse)
        def login_page(request: Request):
            return templates.TemplateResponse(request=request, name='login.html', context={'csrf_token': app.state.csrf})

        @app.post('/api/login')
        def login(body: dict, request: Request):
            token, status = auth.login(body.get('username'), body.get('password'), request.state.visitor_ip)
            if token is None:
                return JSONResponse({'detail': '登录尝试过多，请稍后重试' if status == 429 else '账号或密码错误'}, status_code=status)
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
        return state(store, request.path_params.get('site_id', 'default'))

    @routes.put('/config')
    def update_config(body: ConfigUpdate, request: Request, store=Depends(site_store)):
        store.save_config(body.config, body.revision)
        return state(store, request.path_params.get('site_id', 'default'))

    @routes.post('/upload/{slot}')
    async def upload(slot: Slot, request: Request, name: str = Query(min_length=1, max_length=200), store=Depends(site_store)):
        data = await request.body()
        # CPU/disk work in worker thread keeps health and UI requests responsive.
        from starlette.concurrency import run_in_threadpool
        version = await run_in_threadpool(import_content, data, name, store.pages)
        await run_in_threadpool(store.add_version, slot, version)
        return {'version': version | {'slot': slot}}

    @routes.post('/publish/{slot}')
    def publish(slot: Slot, body: VersionChoice, store=Depends(site_store)):
        store.publish(slot, body.version_id)
        return {'ok': True}

    @routes.post('/preview')
    def preview(body: VersionChoice, request: Request, store=Depends(site_store)):
        if not store.version(body.version_id):
            raise HTTPException(404, '版本不存在')
        expiry = int(time.time()) + 300
        material = f'{expiry}:{body.version_id}'
        signature = hmac.new(store.secret, material.encode(), hashlib.sha256).hexdigest()
        site_id = request.path_params.get('site_id', 'default')
        prefix = '' if site_id == 'default' else f'/_sites/{site_id}'
        return {'url': f'{target_url}{prefix}/_preview/{expiry}/{body.version_id}/{signature}/index.html'}

    @routes.post('/links')
    def add_links(body: LinkInput, store=Depends(site_store)):
        store.add_links(body.slot, body.urls)
        return {'ok': True}

    @routes.delete('/links/{link_id}')
    def delete_link(link_id: int, store=Depends(site_store)):
        store.delete_link(link_id)
        return {'ok': True}

    @routes.post('/counters/reset')
    def reset_counts(store=Depends(site_store)):
        store.reset_counts()
        return {'ok': True}

    @routes.post('/simulate')
    def simulate(body: Visitor, store=Depends(site_store)):
        config, _ = store.config()
        return decide(config, body)

    @routes.get('/logs')
    def logs(days: int = Query(default=7, ge=1, le=30), slot: Literal['', 'A', 'B'] = '', page: int = Query(default=1, ge=1, le=10000), store=Depends(site_store)):
        return store.logs(days, slot, page)

    @routes.get('/logs.csv')
    def export_logs(days: int = Query(default=7, ge=1, le=30), slot: Literal['', 'A', 'B'] = '', store=Depends(site_store)):
        fields = ['id', 'created', 'ip', 'country', 'device', 'device_name', 'device_model', 'os', 'browser', 'slot', 'reason', 'path', 'mode']
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(fields)
        for row in store.logs(days, slot, page_size=10000)['items']:
            details = row.get('device_details') or {}
            row.update(device_name=details.get('device', ''), device_model=details.get('model', ''), os=details.get('os', ''), browser=details.get('browser', ''))
            values = [str(row.get(key) or '') for key in fields]
            writer.writerow(["'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for value in values])
        return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="ab-lab-visits.csv"'})

    @routes.get('/audit')
    def audit(store=Depends(site_store)):
        return {'items': store.audit()}

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


def content_response(store, version_id, path, method='GET'):
    if not version_id:
        return HTMLResponse('<!doctype html><meta charset="utf-8"><h1>页面尚未发布</h1><p>请在管理后台为当前槽位导入并发布内容。</p>', status_code=503)
    try:
        file = resolve_file(store.pages / version_id, path)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '资源不存在') from None
    mime = {'.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}.get(file.suffix.lower()) or mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
    body = file.read_bytes() if method != 'HEAD' else b''
    return Response(body, media_type=mime, headers={'Content-Length': str(file.stat().st_size)})


def create_target(data_dir, port=8766, deployment=None, registry_dir=None):
    app, store = base_app(), Store(data_dir)
    registry = Registry(data_dir, deployment.host(False) if deployment else '', registry_dir=registry_dir)
    app.add_middleware(LocalBoundary, port=port, deployment=deployment, registry=registry)

    def site_store(request: Request):
        return registry.store(request.state.site_id)

    @app.api_route('/_preview/{expiry}/{version_id}/{signature}/{path:path}', methods=['GET', 'HEAD'])
    def preview(expiry: int, version_id: str, signature: str, path: str, request: Request, store=Depends(site_store)):
        material = f'{expiry}:{version_id}'
        expected = hmac.new(store.secret, material.encode(), hashlib.sha256).hexdigest()
        if expiry < time.time() or expiry > time.time() + 301 or not secrets.compare_digest(signature, expected) or not store.version(version_id):
            raise HTTPException(404, '预览不存在或已过期')
        redirect = directory_redirect(store, [version_id], path, request)
        if redirect is not None:
            return redirect
        return content_response(store, version_id, path, request.method)

    @app.api_route('/{path:path}', methods=['GET', 'HEAD'])
    def target(path: str, request: Request, store=Depends(site_store)):
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
                return redirect
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
                return RedirectResponse(link['url'], status_code=302)
            return HTMLResponse('<meta charset="utf-8"><h1>当前槽位尚未配置链接</h1>', status_code=503)
        return content_response(store, store.slots()[slot], path, request.method)

    return app
