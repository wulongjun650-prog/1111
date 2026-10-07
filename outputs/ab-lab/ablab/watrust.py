"""Read one WhatsApp send screen on a VMOS cloud phone and classify it.

The phone opens the public send link, the UI dump is read, then Back is pressed.
Nothing taps 继续聊天, sends a message, or reads the address book.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOST = 'https://api.vmoscloud.com'
API_HOST = 'api.vmoscloud.com'
SERVICE = 'armcloud-paas'
PUBLIC_ORIGIN = 'https://hhucuq.top'
WHATSAPP_PACKAGE = 'com.whatsapp'
DUMP_PATH = '/sdcard/ab-wa-trust.xml'
OPEN_TIMEOUT = 20
SCREEN_WAIT = 45

TRUST_TITLE = ('你信任此用户吗', '你信任此用戶嗎')
CONTINUE_CHAT = ('继续聊天', '繼續聊天')
CANCEL_CHAT = ('取消聊天',)
COMPOSER = ('com.whatsapp:id/entry', '输入消息', '輸入訊息', 'Type a message', 'type a message')


class VmosError(Exception):
    pass


def v2_sign(secret, timestamp, path, body):
    return hashlib.sha256(f'{secret}{timestamp}{path}{body}'.encode()).hexdigest()


def v4_authorization(access_key, secret_key, x_date, body):
    """HMAC used by the live VMOS gateway. The published SHA-256 header is rejected there."""
    content_type = 'application/json;charset=UTF-8'
    signed_headers = 'content-type;host;x-content-sha256;x-date'
    content_hash = hashlib.sha256(body.encode()).hexdigest()
    canonical = (
        f'host:{API_HOST}\n'
        f'x-date:{x_date}\n'
        f'content-type:{content_type}\n'
        f'signedHeaders:{signed_headers}\n'
        f'x-content-sha256:{content_hash}'
    )
    short_date = x_date[:8]
    scope = f'{short_date}/{SERVICE}/request'
    string_to_sign = 'HMAC-SHA256\n' + x_date + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
    key = hmac.new(secret_key.encode(), short_date.encode(), hashlib.sha256).digest()
    key = hmac.new(key, SERVICE.encode(), hashlib.sha256).digest()
    key = hmac.new(key, b'request', hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f'HMAC-SHA256 Credential={access_key}, SignedHeaders={signed_headers}, Signature={signature}'


def send_url(phone):
    if not re.fullmatch(r'\d{8,15}', phone or ''):
        raise ValueError('WhatsApp 号码需为 8 到 15 位数字')
    return f'https://api.whatsapp.com/send/?phone={phone}&text&type=phone_number&app_absent=0'


def _quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def _resolve_arg(upload_url, resolve_ip):
    host = urllib.parse.urlparse(upload_url).hostname or ''
    if not host or not re.fullmatch(r'(?:\d{1,3}\.){3}\d{1,3}', resolve_ip or ''):
        return ''
    try:
        if not ipaddress.ip_address(resolve_ip).is_global:
            return ''
    except ValueError:
        return ''
    return f' --resolve {_quote(f"{host}:443:{resolve_ip}")}'


def open_script(phone, upload_url, resolve_ip=''):
    # One shell command. && stops a failed launch from uploading whatever was already on screen.
    # The dump is posted to the admin. The task API does not return shell output on this account.
    url = send_url(phone)
    target = _quote(upload_url)
    header = _quote('Content-Type: text/plain')
    direct = _resolve_arg(upload_url, resolve_ip)
    upload = (
        f'(curl -fsS -m 20{direct} -o /dev/null -X POST --data-binary @{DUMP_PATH} -H {header} {target}'
        f' || curl -fsS -m 20 -o /dev/null -X POST --data-binary @{DUMP_PATH} -H {header} {target}'
        f' || wget -q -O /dev/null --post-file={DUMP_PATH} {target})'
    )
    return (
        f'am start -a android.intent.action.VIEW -d {_quote(url)} -p {WHATSAPP_PACKAGE}'
        f' && sleep 8 && uiautomator dump {DUMP_PATH} && {upload}'
    )


def back_script():
    return f'input keyevent 4 && rm -f {DUMP_PATH}'


def classify_screen(text):
    if not isinstance(text, str) or not text.strip():
        return 'unconfirmed'
    if any(title in text for title in TRUST_TITLE) or (any(word in text for word in CONTINUE_CHAT) and any(word in text for word in CANCEL_CHAT)):
        return 'trust'
    if any(marker in text for marker in COMPOSER):
        return 'clear'
    return 'unconfirmed'


def _rows(payload):
    data = payload.get('data') if isinstance(payload, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _status(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def urllib_transport(method, url, headers, body):
    request = urllib.request.Request(url, data=body.encode(), headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=OPEN_TIMEOUT) as response:
            payload = response.read(1_000_000)
        return json.loads(payload.decode())
    except Exception:
        raise VmosError('cloud phone request failed') from None


class VmosClient:
    def __init__(self, access_key, secret_key, pad_code, host=HOST, transport=None, clock=None, sleep=None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.pad_code = pad_code
        self.host = host.rstrip('/')
        self.transport = transport or urllib_transport
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep

    def post(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        x_date = datetime.fromtimestamp(int(self.clock()), timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        headers = {
            'content-type': 'application/json;charset=UTF-8',
            'x-date': x_date,
            'x-host': API_HOST,
            'authorization': v4_authorization(self.access_key, self.secret_key, x_date, body),
        }
        payload = self.transport('POST', self.host + path, headers, body)
        if not isinstance(payload, dict) or payload.get('code') != 200:
            raise VmosError('cloud phone request failed')
        return payload

    def submit(self, script):
        started = self.post('/vcpcloud/api/padApi/asyncCmd', {'padCodes': [self.pad_code], 'scriptContent': script})
        rows = _rows(started)
        if not rows or not rows[0].get('taskId'):
            raise VmosError('cloud phone request failed')
        if _status(rows[0].get('vmStatus')) == 0:
            raise VmosError('cloud phone offline')
        return rows[0]['taskId']


class CloudPhone:
    def __init__(self, client, origin=PUBLIC_ORIGIN, resolve_ip='', wait_timeout=SCREEN_WAIT):
        self.client = client
        self.origin = origin.rstrip('/')
        self.resolve_ip = resolve_ip
        self.wait_timeout = wait_timeout
        self._pending = {}
        self._pending_lock = threading.Lock()

    def __call__(self, phone):
        return self.read(phone)

    def deliver(self, token, text):
        with self._pending_lock:
            slot = self._pending.get(token)
        if slot is None:
            return False
        slot['text'] = text
        slot['event'].set()
        return True

    def read(self, phone):
        token = secrets_token()
        slot = {'event': threading.Event(), 'text': None}
        with self._pending_lock:
            self._pending[token] = slot
        try:
            upload = f'{self.origin}/api/wa-trust/screen/{token}'
            self.client.submit(open_script(phone, upload, self._ip()))
            if not slot['event'].wait(self.wait_timeout):
                raise VmosError('cloud phone command timed out')
            return '' if slot['text'] is None else slot['text']
        finally:
            with self._pending_lock:
                self._pending.pop(token, None)
            try:
                self.client.submit(back_script())
            except Exception:
                pass

    def _ip(self):
        if self.resolve_ip != 'auto':
            return self.resolve_ip
        self.resolve_ip = global_ip()
        return self.resolve_ip


class TrustChecker:
    def __init__(self, device=None):
        self.device = device
        self._lock = threading.Lock()

    def deliver(self, token, text):
        deliver = getattr(self.device, 'deliver', None)
        if deliver is None:
            return False
        return deliver(token, text)

    def check(self, phone, persist):
        if not self._lock.acquire(blocking=False):
            return {'busy': True}
        try:
            outcome = self._read(phone)
            return {'busy': False, 'number': persist(outcome), 'status': outcome['status']}
        finally:
            self._lock.release()

    def _read(self, phone):
        if self.device is None:
            return {'status': 'unconfirmed', 'reason': 'unconfigured'}
        try:
            text = self.device(phone)
        except Exception:
            return {'status': 'unconfirmed', 'reason': 'unread'}
        status = classify_screen(text)
        if status == 'trust':
            reason = 'trust'
        elif status == 'clear':
            reason = 'clear'
        elif not isinstance(text, str) or not text.strip():
            reason = 'empty'
        else:
            reason = 'unreadable'
        return {'status': status, 'reason': reason}


def secrets_token():
    import secrets
    return secrets.token_urlsafe(24)


def global_ip():
    sock = socket.socket()
    try:
        sock.settimeout(2)
        sock.connect(('1.1.1.1', 443))
        text = sock.getsockname()[0]
    except Exception:
        return ''
    finally:
        sock.close()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return ''
    return text if address.is_global else ''


def device_from_environ(environ=None):
    environ = os.environ if environ is None else environ
    access = environ.get('AB_VMOS_ACCESS_KEY', '').strip()
    secret = environ.get('AB_VMOS_SECRET_KEY', '').strip()
    pad = environ.get('AB_VMOS_PAD_CODE', '').strip()
    if not access or not secret or not pad:
        return None
    host = environ.get('AB_VMOS_API_HOST', HOST).strip() or HOST
    origin = environ.get('AB_TRUST_PUBLIC_ORIGIN', PUBLIC_ORIGIN).strip() or PUBLIC_ORIGIN
    return CloudPhone(VmosClient(access, secret, pad, host=host), origin=origin, resolve_ip='auto')
