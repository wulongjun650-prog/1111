"""Bounded, read-only public destination checks; no invitation is joined.

Invite pages are evidence of the current public preview only. Login, app-only
links, client-rendered previews and phone-registration checks remain unknown.
"""
from html.parser import HTMLParser
import http.client
import ipaddress
from queue import Empty, Queue
import re
import socket
import ssl
from threading import Thread, Timer
import time
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit


TIMEOUT = 9
MAX_BODY = 512 * 1024
REDIRECTS = {301, 302, 303, 307, 308}


def _public_ip(value):
    address = ipaddress.ip_address(value)
    if ('%' in str(address) or not address.is_global or address.is_reserved or address.is_multicast
            or address.is_loopback or address.is_link_local or address.is_unspecified):
        raise ValueError('non-public address')
    return str(address)


def normalize_http_url(value):
    """Validate saved links without DNS or network I/O; check() vets DNS too."""
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(c) <= 32 or ord(c) == 127 or c.isspace() for c in value)
            or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in unquote(value))):
        raise ValueError('链接含非法字符或过长')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or '@' in parsed.netloc:
        raise ValueError('仅支持不含账号密码的 HTTP(S) 完整链接')
    host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').lower()
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
    if port not in (80, 443) or '%' in host:
        raise ValueError('仅支持公网 HTTP(S) 的 80 或 443 端口')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split('.')
        if (len(labels) < 2 or host == 'localhost' or host.endswith(('.localhost', '.local'))
                or len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
            raise ValueError('链接域名无效或属于本地网络') from None
        authority = host
    else:
        host = _public_ip(address)
        authority = '[' + host + ']' if address.version == 6 else host
    if port != (443 if parsed.scheme == 'https' else 80):
        authority += ':' + str(port)
    path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    return urlunsplit((parsed.scheme, authority, path, query, parsed.fragment))


def _platform(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').rstrip('.').lower()
        if parsed.scheme == 'whatsapp' or host == 'wa.me' or host == 'whatsapp.com' or host.endswith('.whatsapp.com'):
            return 'WhatsApp'
        if parsed.scheme == 'bandapp' or host == 'band.us' or host.endswith('.band.us'):
            return 'BAND'
    except (ValueError, TypeError):
        pass
    return '网站'


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('deadline')
    return remaining


def _bounded(function, args, deadline):
    """OS DNS has no timeout API; do not wait for it past the shared budget."""
    result = Queue(maxsize=1)

    def run():
        try:
            result.put((True, function(*args)))
        except Exception as error:
            result.put((False, error))

    remaining = _remaining(deadline)
    Thread(target=run, daemon=True).start()
    try:
        success, value = result.get(timeout=remaining)
    except Empty:
        raise TimeoutError('deadline') from None
    _remaining(deadline)
    if not success:
        raise value
    return value


def _resolve(host, port):
    return list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)))


def _fetch(url, vetted_ip, deadline):
    """Use the vetted numeric IP; retain the URL hostname for TLS and Host."""
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    stream = response = None

    def abort():
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()

    timer = Timer(_remaining(deadline), abort)
    timer.daemon = True
    timer.start()
    try:
        stream = socket.create_connection((vetted_ip, port), timeout=_remaining(deadline))
        stream.settimeout(_remaining(deadline))
        if parsed.scheme == 'https':
            stream = ssl.create_default_context().wrap_socket(stream, server_hostname=parsed.hostname)
        stream.settimeout(_remaining(deadline))
        target = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        request = (f'GET {target} HTTP/1.1\r\nHost: {parsed.netloc}\r\n'
                   'User-Agent: AB-Lab-LinkCheck/1.0\r\nAccept: text/html,*/*;q=0.1\r\n'
                   'Accept-Encoding: identity\r\nConnection: close\r\n\r\n').encode('ascii')
        stream.sendall(request)
        response = http.client.HTTPResponse(stream, method='GET')
        response.begin()
        headers = {}
        for name, value in response.getheaders():
            key = name.lower()
            if key in ('location', 'content-encoding', 'content-type', 'content-length'):
                if key in headers:
                    raise ValueError('ambiguous response headers')
                headers[key] = value
        _remaining(deadline)
        if response.status in REDIRECTS or response.status in (401, 403, 404, 410, 429) or response.status >= 500:
            return response.status, headers, b''
        if (headers.get('content-encoding', 'identity').lower() not in ('', 'identity')
                or int(headers.get('content-length', '0')) > MAX_BODY):
            raise ValueError('unsupported response body')
        stream.settimeout(_remaining(deadline))
        body = response.read(MAX_BODY + 1)
        _remaining(deadline)
        return response.status, headers, body
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        if stream is not None:
            stream.close()


def _text(value):
    return ' '.join(value.lower().replace('’', "'").split()).strip(' .!?。！')


class _Page(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.stack, self.blocked = [], False
        self.texts, self.links, self.meta = [], [], {}
        self.feed(body.decode('utf-8', errors='replace'))

    def handle_starttag(self, tag, attrs):
        values = {name: value or '' for name, value in attrs}
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in ('script', 'style', 'noscript', 'template')
                  or 'hidden' in values or values.get('aria-hidden', '').lower() == 'true'
                  or bool(re.search(r'(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b', values.get('style', ''), re.I)))
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            if len(self.stack) >= 128:
                raise ValueError('excessive HTML nesting')
            self.stack.append((tag, hidden))
        if hidden:
            return
        if ((tag == 'input' and values.get('type', '').lower() == 'password')
                or {'h-captcha', 'g-recaptcha'}.intersection(values.get('class', '').lower().split())):
            self.blocked = True
        if tag == 'iframe':
            try:
                source = urlsplit(values.get('src', ''))
                host = source.hostname or ''
                if (host == 'hcaptcha.com' or host.endswith('.hcaptcha.com') or host == 'challenges.cloudflare.com'
                        or ((host == 'google.com' or host.endswith('.google.com')) and source.path.startswith('/recaptcha/'))):
                    self.blocked = True
            except ValueError:
                pass
        if tag == 'meta':
            self.meta[(values.get('property') or values.get('name') or '').lower()] = values.get('content', '')
        if tag == 'a' and values.get('href'):
            self.links.append(values['href'])

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        if not (self.stack and self.stack[-1][1]) and value.strip():
            self.texts.append(_text(value))


def _invite_kind(url):
    parsed = urlsplit(url)
    if parsed.hostname == 'chat.whatsapp.com' and re.fullmatch(r'/[A-Za-z0-9_-]{8,128}/?', parsed.path):
        return 'WhatsApp'
    if _platform(url) == 'BAND' and re.fullmatch(r'/n/[A-Za-z0-9_-]+/?', parsed.path):
        return 'BAND'
    return None


def _positive_whatsapp(page, url):
    title = _text(page.meta.get('og:title', ''))
    if not title or title in ('whatsapp', 'whatsapp group invite', 'group invite'):
        return False
    if 'whatsapp group invite' not in page.texts or not any(text in ('join group', 'join chat') for text in page.texts):
        return False
    code = urlsplit(url).path.strip('/')
    for link in page.links:
        try:
            parsed = urlsplit(link)
            if parsed.scheme == 'whatsapp' and parsed.netloc == 'chat' and parse_qs(parsed.query).get('code') == [code]:
                return True
        except ValueError:
            pass
    return False


class LinkChecker:
    """Injected resolver(host, port) and fetch(url, vetted_ip, deadline) are optional.

    The fetch returns (HTTP status, headers dict, body bytes); neither headers nor
    bodies are exposed in the result. The built-in fetch has no proxy/cookie jar.
    """
    def __init__(self, resolver=None, fetch=None):
        self.resolver = resolver or _resolve
        self.fetch = fetch or _fetch

    def check(self, url):
        platform = _platform(url)
        result = dict(status='unknown', platform=platform, detail='无法确认链接状态',
                      checked_at=float(time.time()), http_status=None, final_url=None)
        deadline = time.monotonic() + TIMEOUT
        try:
            current = normalize_http_url(url)
            for hop in range(4):
                parsed = urlsplit(current)
                try:
                    address = ipaddress.ip_address(parsed.hostname)
                except ValueError:
                    answers = _bounded(self.resolver, (parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)), deadline)
                    if not answers:
                        raise ValueError('no DNS addresses')
                    vetted = [_public_ip(answer) for answer in answers]
                else:
                    vetted = [_public_ip(address)]
                status, headers, body = _bounded(self.fetch, (current, vetted[0], deadline), deadline)
                if type(status) is not int or not 100 <= status <= 599 or not isinstance(headers, dict) or not isinstance(body, bytes):
                    raise ValueError('invalid response')
                result.update(http_status=status, final_url=current)
                if platform == '网站':
                    result['platform'] = _platform(current)
                headers = {key.lower(): value for key, value in headers.items()}
                if status in REDIRECTS:
                    if hop == 3 or not isinstance(headers.get('location'), str):
                        raise ValueError('redirect budget or missing location')
                    location = headers['location']
                    # Validate raw Location before urljoin, which silently strips controls.
                    if any(ord(c) <= 32 or ord(c) == 127 for c in location) or '\\' in location:
                        raise ValueError('unsafe redirect')
                    current = normalize_http_url(urljoin(current, location))
                    continue
                if status in (404, 410):
                    result.update(status='abnormal', detail='目标页面不存在或已移除')
                    return result
                if not 200 <= status < 300:
                    result['detail'] = '目标拒绝访问、限流或暂时不可用，无法确认'
                    return result
                if len(body) > MAX_BODY or headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
                    raise ValueError('unsupported response body')
                page = _Page(body)
                if page.blocked or any(text in {'verify you are human', 'sign in to continue', 'log in to continue', 'just a moment',
                                'checking your browser', 'please enable javascript and cookies to continue',
                                'internal server error', 'service unavailable', 'bad gateway', 'page not found', '404 not found',
                                '请登录后继续', '请完成人机验证'} for text in page.texts):
                    result['detail'] = '目标需要登录、人机验证或返回错误页面，无法确认'
                    return result
                invite = _invite_kind(current)
                invalid = {'this invite link is not valid', 'this invite link has been reset',
                           'this invite link has expired', 'this group is no longer available',
                           '邀请链接无效', '邀请链接已过期', '초대 링크가 만료되었습니다',
                           'this invitation link has expired', 'this invitation link is invalid',
                           'this invitation has expired',
                           "you can't join the group or community because the invite link was reset"}
                if invite and any(text in invalid for text in page.texts):
                    result.update(status='abnormal', detail='平台页面明确提示邀请无效或已过期')
                elif invite == 'WhatsApp' and _positive_whatsapp(page, current):
                    result.update(status='normal', detail='检测到群名称和匹配的加入入口，邀请页面可用；实际加入仍由平台确认')
                elif result['platform'] != '网站':
                    result['detail'] = '平台页面可访问，但无法确认邀请有效或号码已注册'
                else:
                    result.update(status='normal', detail='本次连接检测成功；仅确认页面可访问')
                return result
        except socket.gaierror as error:
            if error.errno == socket.EAI_NONAME:
                result.update(status='abnormal', detail='目标域名不存在')
            else:
                result['detail'] = '域名解析暂时失败，无法确认'
        except (OSError, http.client.HTTPException, ValueError, TypeError, UnicodeError):
            result['detail'] = '链接不符合公网检测条件，或检测超时、连接失败，无法确认'
        return result
