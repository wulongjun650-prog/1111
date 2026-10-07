"""Read one WhatsApp send screen on a VMOS cloud phone and classify it.

The phone opens the public send link, the UI dump is read, then Back is pressed.
Nothing taps 继续聊天, sends a message, or reads the address book.
"""

import hashlib
import json
import os
import re
import threading
import time
import urllib.request

HOST = 'https://api.vmoscloud.com'
WHATSAPP_PACKAGE = 'com.whatsapp'
DUMP_PATH = '/sdcard/ab-wa-trust.xml'
OPEN_TIMEOUT = 20

TRUST_TITLE = ('你信任此用户吗', '你信任此用戶嗎')
CONTINUE_CHAT = ('继续聊天', '繼續聊天')
CANCEL_CHAT = ('取消聊天',)
COMPOSER = ('com.whatsapp:id/entry', '输入消息', '輸入訊息', 'Type a message', 'type a message')


class VmosError(Exception):
    pass


def v2_sign(secret, timestamp, path, body):
    return hashlib.sha256(f'{secret}{timestamp}{path}{body}'.encode()).hexdigest()


def send_url(phone):
    if not re.fullmatch(r'\d{8,15}', phone or ''):
        raise ValueError('WhatsApp 号码需为 8 到 15 位数字')
    return f'https://api.whatsapp.com/send/?phone={phone}&text&type=phone_number&app_absent=0'


def _quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def open_script(phone):
    # One shell command. && stops a failed launch from dumping whatever was already on screen.
    url = send_url(phone)
    return (
        f'am start -a android.intent.action.VIEW -d {_quote(url)} -p {WHATSAPP_PACKAGE}'
        f' && sleep 5 && uiautomator dump {DUMP_PATH} && cat {DUMP_PATH}'
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

    def post(self, path, payload, sign_body):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        signed = body if sign_body else ''
        timestamp = str(int(self.clock()))
        headers = {
            'X-Access-Key': self.access_key,
            'X-Timestamp': timestamp,
            'X-Sign': v2_sign(self.secret_key, timestamp, path, signed),
            'Content-Type': 'application/json',
        }
        payload = self.transport('POST', self.host + path, headers, body)
        if not isinstance(payload, dict) or payload.get('code') != 200:
            raise VmosError('cloud phone request failed')
        return payload

    def run_script(self, script, timeout=OPEN_TIMEOUT):
        started = self.post('/vcpcloud/api/padApi/asyncCmd', {'padCodes': [self.pad_code], 'scriptContent': script}, False)
        rows = _rows(started)
        if not rows or not rows[0].get('taskId'):
            raise VmosError('cloud phone request failed')
        if _status(rows[0].get('vmStatus')) == 0:
            raise VmosError('cloud phone offline')
        task_id = rows[0]['taskId']
        deadline = self.clock() + timeout
        while True:
            detail = self.post('/vcpcloud/api/padApi/padTaskDetail', {'taskIds': [task_id]}, True)
            found = _rows(detail)
            row = next((item for item in found if item.get('taskId') == task_id), None)
            if row is None and len(found) == 1:
                row = found[0]
            status = _status(row.get('taskStatus')) if row else None
            if status == 3:
                result = row.get('taskResult')
                if result is None:
                    result = row.get('cmdResult')
                return '' if result is None else str(result)
            if status in (-1, -2, -3, -4):
                raise VmosError('cloud phone command failed')
            if self.clock() > deadline:
                raise VmosError('cloud phone command timed out')
            self.sleep(1)


class CloudPhone:
    def __init__(self, client):
        self.client = client

    def read(self, phone):
        try:
            return self.client.run_script(open_script(phone))
        finally:
            try:
                self.client.run_script(back_script())
            except Exception:
                pass


class TrustChecker:
    def __init__(self, device=None):
        self.device = device
        self._lock = threading.Lock()

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


def device_from_environ(environ=None):
    environ = os.environ if environ is None else environ
    access = environ.get('AB_VMOS_ACCESS_KEY', '').strip()
    secret = environ.get('AB_VMOS_SECRET_KEY', '').strip()
    pad = environ.get('AB_VMOS_PAD_CODE', '').strip()
    if not access or not secret or not pad:
        return None
    host = environ.get('AB_VMOS_API_HOST', HOST).strip() or HOST
    return CloudPhone(VmosClient(access, secret, pad, host=host)).read
