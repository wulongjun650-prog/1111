"""Open one local browser window and read the work-order list that page requests.

The view password is typed into the page. It is not returned and not written into the browser profile.
"""

import atexit
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .desk import DeskError, ticket_code


GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
LIST_PATH = '/webApi/accountshow/list'
MAX_FRAME = 8_000_000


def sandbox_off(euid, status):
    """Chrome cannot start its sandbox as root, or inside the hardened admin service."""
    if euid == 0:
        return True
    for line in status.splitlines():
        if line.startswith('NoNewPrivs:') and line.split(':', 1)[1].strip() == '1':
            return True
    return False


def chrome_executable():
    chosen = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE', '').strip()
    if chosen:
        return chosen
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'msedge'):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        '/usr/local/bin/google-chrome',
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return ''


def readable_list(payload):
    """A list response is usable once the page has actually returned the account list."""
    if not isinstance(payload, dict) or payload.get('code') not in (None, 1):
        return False
    body = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    if not isinstance(body, dict):
        return False
    return isinstance(body.get('items'), list) or isinstance(body.get('accounts'), list) or 'online' in body or isinstance(body.get('shareStatistics'), dict)


def _fill_script(password):
    return """(() => {
      const password = %s;
      const box = document.querySelector('input[type="password"], input[placeholder*="密码"]');
      if (!box) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(box, password);
      box.dispatchEvent(new Event('input', {bubbles: true}));
      box.dispatchEvent(new Event('change', {bubbles: true}));
      const button = Array.from(document.querySelectorAll('button, [role="button"]')).find(item => /确定|确认|查看|提交|登录|进入/.test(item.textContent || ''));
      if (button) button.click();
      else if (box.form) box.form.requestSubmit ? box.form.requestSubmit() : box.form.submit();
      return true;
    })()""" % json.dumps(password)


class WorkOrderWindow:
    def __init__(self, profile_dir, executable='', headless=False, timeout=50):
        self.profile_dir = Path(profile_dir)
        self.executable = executable or chrome_executable()
        self.headless = headless
        self.timeout = timeout
        self._lock = threading.Lock()
        self._proc = None
        self._display_proc = None
        self._display = ''
        self._sock = None
        self._buffer = b''
        self._ident = 0
        self._list_ids = set()
        self._ready_id = ''
        atexit.register(self.close)

    def close(self):
        with self._lock:
            self._shutdown()

    def __call__(self, url, password, name=''):
        ticket_code(url)
        parts = urlsplit(str(url))
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise DeskError('工单链接不对')
        with self._lock:
            try:
                self._ensure()
                return self._read(str(url), str(password or ''))
            except DeskError:
                raise
            except (OSError, TimeoutError, json.JSONDecodeError, ValueError):
                self._shutdown()
                raise DeskError('工单窗口打不开') from None

    def _ensure(self):
        if self._proc and self._proc.poll() is None and self._sock:
            return
        self._shutdown()
        if not self.executable:
            raise DeskError('工单窗口打不开')
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        default = self.profile_dir / 'Default'
        default.mkdir(parents=True, exist_ok=True)
        preferences = default / 'Preferences'
        if not preferences.exists():
            preferences.write_text(json.dumps({
                'credentials_enable_service': False,
                'profile': {'password_manager_enabled': False},
            }), encoding='utf-8')
        args = [
            self.executable,
            f'--user-data-dir={self.profile_dir}',
            '--remote-debugging-port=0',
            '--remote-debugging-address=127.0.0.1',
            '--remote-allow-origins=*',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-sync',
            '--window-size=1100,800',
            'about:blank',
        ]
        if self.headless:
            args.insert(-1, '--headless=new')
        display = self._start_display()
        if os.name != 'nt':
            try:
                status = Path('/proc/self/status').read_text(encoding='utf-8', errors='replace')
            except OSError:
                status = ''
            try:
                euid = os.geteuid()
            except AttributeError:
                euid = -1
            if sandbox_off(euid, status):
                args[1:1] = ['--no-sandbox', '--disable-dev-shm-usage']
        env = os.environ.copy()
        if display:
            env['DISPLAY'] = display
        self._proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        port_file = self.profile_dir / 'DevToolsActivePort'
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise DeskError('工单窗口打不开')
            if port_file.exists() and port_file.stat().st_size:
                break
            time.sleep(0.1)
        else:
            self._shutdown()
            raise DeskError('工单窗口打不开')
        port = int(port_file.read_text(encoding='utf-8').splitlines()[0])
        page = self._page_target(port)
        self._connect(page['webSocketDebuggerUrl'])
        self._call('Network.enable')
        self._call('Page.enable')
        self._call('Runtime.enable')

    def _page_target(self, port):
        deadline = time.time() + 10
        while time.time() < deadline:
            targets = self._http_json(port, 'GET', '/json/list')
            page = next((item for item in targets if item.get('type') == 'page' and item.get('webSocketDebuggerUrl')), None)
            if page:
                return page
            time.sleep(0.1)
        created = None
        for method in ('PUT', 'GET'):
            try:
                created = self._http_json(port, method, '/json/new?about:blank')
            except (OSError, ValueError):
                continue
            if isinstance(created, dict) and created.get('webSocketDebuggerUrl'):
                return created
        raise DeskError('工单窗口打不开')

    def _http_json(self, port, method, path):
        request = Request(f'http://127.0.0.1:{port}{path}', method=method)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def _connect(self, ws_url):
        parts = urlsplit(ws_url)
        host = parts.hostname or ''
        if host not in ('127.0.0.1', 'localhost') or not parts.port:
            raise DeskError('工单窗口打不开')
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        sock = socket.create_connection((host, parts.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {host}:{parts.port}\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            'Sec-WebSocket-Version: 13\r\n\r\n'
        ).encode())
        raw = b''
        while b'\r\n\r\n' not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise DeskError('工单窗口打不开')
            raw += chunk
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        header = raw.split(b'\r\n\r\n', 1)[0].decode('latin1')
        if '101' not in header.split('\r\n', 1)[0] or accept not in header:
            sock.close()
            raise DeskError('工单窗口打不开')
        self._sock = sock
        self._buffer = raw.split(b'\r\n\r\n', 1)[1]

    def _read(self, url, password):
        self._list_ids.clear()
        self._ready_id = ''
        self._call('Page.navigate', {'url': url})
        deadline = time.time() + self.timeout
        fills = 0
        next_fill = time.time()
        while time.time() < deadline:
            if self._ready_id:
                request_id = self._ready_id
                self._ready_id = ''
                try:
                    payload = self._body(request_id)
                except DeskError:
                    payload = None
                if readable_list(payload):
                    return payload
            if password and fills < 4 and time.time() >= next_fill:
                if self._fill(password):
                    fills += 1
                next_fill = time.time() + 2
            message = self._recv(min(deadline, time.time() + 0.4))
            if message and message.get('method'):
                self._on_event(message)
        raise DeskError('工单窗口里还没读到数据，请在弹出的窗口完成验证')

    def _fill(self, password):
        result = self._call('Runtime.evaluate', {'expression': _fill_script(password), 'returnByValue': True})
        return bool((result.get('result') or {}).get('value'))

    def _body(self, request_id):
        result = self._call('Network.getResponseBody', {'requestId': request_id})
        text = result.get('body') or ''
        if result.get('base64Encoded'):
            text = base64.b64decode(text).decode('utf-8', 'replace')
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _on_event(self, message):
        method = message.get('method')
        params = message.get('params') or {}
        if method == 'Network.responseReceived':
            response = params.get('response') or {}
            if response.get('status') == 200 and LIST_PATH in str(response.get('url') or ''):
                self._list_ids.add(params.get('requestId'))
        elif method == 'Network.loadingFinished' and params.get('requestId') in self._list_ids:
            self._ready_id = params.get('requestId')

    def _call(self, method, params=None, timeout=10):
        self._ident += 1
        ident = self._ident
        self._send({'id': ident, 'method': method, 'params': params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self._recv(deadline)
            if not message:
                continue
            if message.get('id') == ident:
                if message.get('error'):
                    raise DeskError('工单窗口打不开')
                return message.get('result') or {}
            if message.get('method'):
                self._on_event(message)
        raise DeskError('工单窗口打不开')

    def _send(self, payload):
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack('!H', length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack('!Q', length))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self._sock.sendall(header + masked)

    def _recv(self, deadline):
        fragments = []
        while time.time() < deadline:
            frame = self._frame(deadline)
            if frame is None:
                return None
            fin, opcode, data = frame
            if opcode == 9:
                self._send_control(0xA, data)
                continue
            if opcode == 8:
                raise DeskError('工单窗口已关闭')
            if opcode in (0, 1):
                fragments.append(data)
                if fin:
                    return json.loads(b''.join(fragments).decode())
        return None

    def _send_control(self, opcode, data):
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self._sock.sendall(bytes([0x80 | opcode, 0x80 | len(data)]) + mask + masked)

    def _frame(self, deadline):
        try:
            header = self._exact(2, deadline)
        except TimeoutError:
            return None
        fin = header[0] & 0x80
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack('!H', self._exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._exact(8, deadline))[0]
        if length > MAX_FRAME:
            raise DeskError('工单数据无法读取')
        if header[1] & 0x80:
            mask = self._exact(4, deadline)
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self._exact(length, deadline)))
        else:
            payload = self._exact(length, deadline) if length else b''
        return fin, opcode, payload

    def _exact(self, size, deadline):
        while len(self._buffer) < size:
            if time.time() >= deadline:
                raise TimeoutError
            self._sock.settimeout(max(0.05, deadline - time.time()))
            chunk = self._sock.recv(65536)
            if not chunk:
                raise DeskError('工单窗口已关闭')
            self._buffer += chunk
        result, self._buffer = self._buffer[:size], self._buffer[size:]
        return result

    def _shutdown(self):
        sock = self._sock
        self._sock = None
        self._buffer = b''
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        proc = self._proc
        self._proc = None
        display = self._display_proc
        self._display_proc = None
        self._display = ''
        for child in (proc, display):
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()

    def _start_display(self):
        if self.headless:
            return ''
        current = os.environ.get('DISPLAY', '').strip()
        if current:
            return current
        if self._display_proc and self._display_proc.poll() is None and self._display:
            return self._display
        xvfb = shutil.which('Xvfb')
        if not xvfb:
            return ''
        number = 99
        socket_path = Path(f'/tmp/.X11-unix/X{number}')
        self._display = f':{number}'
        self._display_proc = subprocess.Popen(
            [xvfb, self._display, '-screen', '0', '1100x800x24', '-nolisten', 'tcp', '-ac'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if self._display_proc.poll() is not None:
                return ''
            if socket_path.exists():
                return self._display
            time.sleep(0.05)
        return self._display
