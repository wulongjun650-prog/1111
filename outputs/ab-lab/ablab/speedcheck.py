"""PageSpeed score plus a network sample from a chosen country.

Google's PageSpeed Insights lab picks North America, Europe, or Asia on its
own. It cannot be pinned to Hong Kong or Korea. The timing row comes from a
Globalping probe in the selected country, and a probe that lands somewhere
else is reported as a miss.
"""

import ipaddress
import json
import logging
import os
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from .models import StrictModel

LOGGER = logging.getLogger('ablab')
REGIONS = {
    'HK': '香港',
    'KR': '韩国',
    'TW': '台湾',
    'JP': '日本',
    'SG': '新加坡',
    'US': '美国',
}
PAGESPEED_NOTE = '评分来自 Google PageSpeed Insights 实验室。实验室在北美、欧洲或亚洲就近选择，不能指定香港或韩国。'
LIGHTHOUSE_NOTE = '这次性能评分由本机 Lighthouse 按 PageSpeed Insights 的算法跑出。打分的机器不在香港或韩国。网速仍是从所选国家测的。'
PROBE_NOTE = '网速从所选国家的探测点发出。选香港就是香港，选韩国就是韩国。'
QUOTA_ERROR = 'PageSpeed 免费额度已用完，这次没有评分。网速结果仍然有效。'
_HOST = re.compile(r'(?i)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+')
_MEASUREMENT = re.compile(r'[A-Za-z0-9_-]{6,80}')
_PRIVATE_SUFFIXES = ('.local', '.localhost', '.internal', '.home', '.lan')


class SpeedCheckInput(StrictModel):
    url: str = Field(min_length=1, max_length=500)
    region: Literal['HK', 'KR', 'TW', 'JP', 'SG', 'US']
    repeats: int = Field(default=1, ge=1, le=3)
    strategy: Literal['mobile', 'desktop'] = 'mobile'


class SpeedBusy(Exception):
    pass


def normalize_target(raw):
    text = raw.strip()
    if not text or len(text) > 500:
        raise ValueError('请填写网址')
    if text.startswith('//'):
        text = 'https:' + text
    elif '://' not in text:
        text = 'https://' + text
    parts = urlsplit(text)
    if parts.scheme not in ('http', 'https'):
        raise ValueError('只支持 http 或 https')
    if parts.username or parts.password:
        raise ValueError('网址不能带账号密码')
    host = (parts.hostname or '').rstrip('.')
    if not host:
        raise ValueError('请填写网站域名')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise ValueError('不能检测内网地址')
        host_text = str(address)
        display_host = f'[{host_text}]' if address.version == 6 else host_text
    else:
        lowered = host.lower()
        if lowered == 'localhost' or lowered.endswith(_PRIVATE_SUFFIXES):
            raise ValueError('不能检测内网地址')
        try:
            host_text = host.encode('idna').decode('ascii')
        except UnicodeError as error:
            raise ValueError('请填写完整域名') from error
        if not _HOST.fullmatch(host_text):
            raise ValueError('请填写完整域名')
        display_host = host_text
    port = parts.port
    if port in (80, 443):
        port = None
    elif port is not None:
        raise ValueError('只检测网站的 80 或 443 端口')
    path = parts.path or '/'
    if not path.startswith('/'):
        path = '/' + path
    if any(ord(char) < 32 or char == ' ' for char in path + parts.query):
        raise ValueError('网址格式不正确')
    url = f'{parts.scheme}://{display_host}{path}'
    if parts.query:
        url += '?' + parts.query
    if len(url) > 500:
        raise ValueError('网址过长')
    return {'url': url, 'host': host_text, 'path': path, 'query': parts.query, 'protocol': 'HTTPS' if parts.scheme == 'https' else 'HTTP', 'port': port}


def _number(value, places, upper):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > upper:
        return None
    return round(float(value), places)


def _text(value, limit):
    if not isinstance(value, str):
        return ''
    cleaned = ''.join(char for char in value if char.isprintable())
    return cleaned.strip()[:limit]


def host_is_public(host):
    """True when every address for this host is a public IP."""
    text = str(host or '').strip().strip('[]')
    if not text:
        return False
    try:
        return ipaddress.ip_address(text).is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(text, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    seen = False
    for info in infos:
        seen = True
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        except ValueError:
            return False
    return seen


class _PublicProxy:
    """Force Chrome through a proxy that refuses private addresses."""

    def __init__(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(('127.0.0.1', 0))
        self._socket.listen(32)
        self._socket.settimeout(0.5)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name='speedcheck-proxy', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(2)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._socket.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), name='speedcheck-tunnel', daemon=True).start()

    def _handle(self, conn):
        remote = None
        try:
            conn.settimeout(15)
            data = b''
            while b'\r\n\r\n' not in data and len(data) < 16384:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            line = data.split(b'\r\n', 1)[0].decode('latin1', 'replace')
            parts = line.split(' ')
            if len(parts) < 2 or not _proxy_target_allowed(parts[0], parts[1]):
                conn.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                return
            host, port = _proxy_endpoint(parts[0], parts[1])
            remote = socket.create_connection((host, port), timeout=15)
            remote.settimeout(15)
            if parts[0] == 'CONNECT':
                conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                _rest = data.split(b'\r\n\r\n', 1)[1]
                if _rest:
                    remote.sendall(_rest)
            else:
                remote.sendall(data)
            _splice(conn, remote)
        except OSError:
            try:
                conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
        finally:
            for item in (remote, conn):
                if item is not None:
                    try:
                        item.close()
                    except OSError:
                        pass


def _proxy_endpoint(method, target):
    if method == 'CONNECT':
        host, _, port_text = target.rpartition(':')
        return host.strip('[]'), int(port_text)
    parts = urlsplit(target)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    return parts.hostname or '', port


def _proxy_target_allowed(method, target):
    try:
        host, port = _proxy_endpoint(method, target)
    except (ValueError, TypeError):
        return False
    if port not in (80, 443) or method not in ('CONNECT', 'GET', 'HEAD'):
        return False
    return host_is_public(host)


def _splice(left, right):
    sockets = [left, right]
    while sockets:
        readable, _, _ = select.select(sockets, [], [], 20)
        if not readable:
            return
        for item in readable:
            other = right if item is left else left
            try:
                chunk = item.recv(16384)
            except OSError:
                return
            if not chunk:
                return
            try:
                other.sendall(chunk)
            except OSError:
                return


def parse_pagespeed(payload):
    if isinstance(payload, dict) and 'categories' in payload and 'lighthouseResult' not in payload:
        payload = {'lighthouseResult': payload, 'analysisUTCTimestamp': payload.get('fetchTime') or ''}
    lighthouse = payload.get('lighthouseResult') if isinstance(payload, dict) else None
    categories = lighthouse.get('categories') if isinstance(lighthouse, dict) else None
    performance = categories.get('performance') if isinstance(categories, dict) else None
    score = performance.get('score') if isinstance(performance, dict) else None
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError('没有性能评分')
    audits = lighthouse.get('audits') if isinstance(lighthouse.get('audits'), dict) else {}

    def metric(name, places=1, upper=120000):
        audit = audits.get(name)
        if not isinstance(audit, dict):
            return None
        return _number(audit.get('numericValue'), places, upper)

    fetched = payload.get('analysisUTCTimestamp')
    return {
        'score': int(round(score * 100)),
        'fcp_ms': metric('first-contentful-paint'),
        'lcp_ms': metric('largest-contentful-paint'),
        'tbt_ms': metric('total-blocking-time'),
        'cls': metric('cumulative-layout-shift', 3, 10),
        'si_ms': metric('speed-index'),
        'fetched_at': fetched if isinstance(fetched, str) and len(fetched) <= 40 else '',
    }


def _content_length(headers):
    if not isinstance(headers, dict):
        return None
    raw = headers.get('content-length', headers.get('Content-Length'))
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, int) and not isinstance(raw, bool):
        text = str(raw)
    elif isinstance(raw, str) and raw.isdigit():
        text = raw
    else:
        return None
    value = int(text)
    return value if value <= 50_000_000 else None


def parse_probe(payload, country, region_label):
    results = payload.get('results') if isinstance(payload, dict) else None
    if not results or not isinstance(results, list):
        return None, '探测没有返回结果'
    item = results[0] if isinstance(results[0], dict) else {}
    result = item.get('result') if isinstance(item.get('result'), dict) else {}
    if result.get('status') == 'failed':
        return None, '探测点没有连上这个网址'
    if result.get('status') != 'finished':
        return None, '探测还没有完成'
    probe = item.get('probe') if isinstance(item.get('probe'), dict) else {}
    actual = _text(probe.get('country'), 2).upper()
    if actual != country:
        where = actual if re.fullmatch(r'[A-Z]{2}', actual) else '未知地区'
        return None, f'探测点落在 {where}，不是{region_label}'
    address = result.get('resolvedAddress')
    try:
        ip = str(ipaddress.ip_address(str(address).strip()))
    except (ValueError, AttributeError):
        ip = ''
    status = result.get('statusCode')
    timings = result.get('timings') if isinstance(result.get('timings'), dict) else {}
    total = _number(timings.get('total'), 1, 120000)
    if total is None:
        return None, '探测点没有返回耗时'
    return {
        'country': country,
        'city': _text(probe.get('city'), 80),
        'network': _text(probe.get('network'), 80),
        'ip': ip,
        'status': status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None,
        'total_ms': total,
        'dns_ms': _number(timings.get('dns'), 1, 120000),
        'tcp_ms': _number(timings.get('tcp'), 1, 120000),
        'tls_ms': _number(timings.get('tls'), 1, 120000),
        'first_byte_ms': _number(timings.get('firstByte'), 1, 120000),
        'download_ms': _number(timings.get('download'), 1, 120000),
        'bytes': _content_length(result.get('headers')),
    }, None


def _lighthouse_binary():
    for candidate in (os.environ.get('AB_LIGHTHOUSE_BIN', '').strip(), '/opt/ab-lab-lighthouse/node_modules/.bin/lighthouse', shutil.which('lighthouse') or ''):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ''


def _chrome_binary():
    for candidate in (os.environ.get('AB_CHROME_PATH', '').strip(), '/usr/bin/google-chrome-stable', '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', shutil.which('google-chrome') or '', shutil.which('chromium') or ''):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ''


def _clean_env():
    hidden = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'PASSWD')
    return {key: value for key, value in os.environ.items() if not any(word in key.upper() for word in hidden)}


class SpeedChecker:
    def __init__(self, transport=None, sleeper=time.sleep, lighthouse=None):
        self.transport = transport
        self.sleeper = sleeper
        # None runs Lighthouse on this machine when Google has no quota.
        # False leaves the Google error as-is. A callable is a test double.
        self.lighthouse = lighthouse
        self._jobs = {}
        self._lock = threading.Lock()

    def start(self, body, owner):
        target = normalize_target(body.url)
        job_id = secrets.token_hex(12)
        job = {
            'id': job_id,
            'owner': owner,
            'status': 'running',
            'error': '',
            'created': time.time(),
            'url': target['url'],
            'host': target['host'],
            'path': target['path'],
            'query': target['query'],
            'protocol': target['protocol'],
            'port': target['port'],
            'region': body.region,
            'region_label': REGIONS[body.region],
            'repeats': body.repeats,
            'strategy': body.strategy,
            'strategy_label': '手机' if body.strategy == 'mobile' else '电脑',
            'completed': 0,
            'runs': [],
        }
        with self._lock:
            if any(item['status'] == 'running' and item['owner'] == owner for item in self._jobs.values()):
                raise SpeedBusy('上一次检测还在进行')
            if sum(item['status'] == 'running' for item in self._jobs.values()) >= 2:
                raise SpeedBusy('已有检测在进行，请稍后再试')
            self._jobs[job_id] = job
            self._prune()
        threading.Thread(target=self._execute, args=(job_id,), name='speedcheck', daemon=True).start()
        with self._lock:
            return self._public(self._jobs[job_id])

    def get(self, job_id, owner):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job['owner'] != owner:
                return None
            return self._public(job)

    def _prune(self):
        finished = sorted((item for item in self._jobs.values() if item['status'] != 'running'), key=lambda item: item['created'])
        while len(self._jobs) > 20 and finished:
            self._jobs.pop(finished.pop(0)['id'], None)

    def _public(self, job):
        runs = [dict(item) for item in job['runs']]
        scores = [item['pagespeed']['score'] for item in runs if item.get('pagespeed')]
        totals = [item['probe']['total_ms'] for item in runs if item.get('probe')]
        summary = None
        if scores or totals:
            summary = {
                'score_avg': round(sum(scores) / len(scores)) if scores else None,
                'score_count': len(scores),
                'total_ms_avg': round(sum(totals) / len(totals), 1) if totals else None,
                'probe_count': len(totals),
            }
        sources = {item['pagespeed'].get('source') for item in runs if item.get('pagespeed')}
        note = LIGHTHOUSE_NOTE if sources == {'lighthouse'} else PAGESPEED_NOTE
        return {
            'id': job['id'],
            'status': job['status'],
            'error': job['error'],
            'url': job['url'],
            'region': job['region'],
            'region_label': job['region_label'],
            'repeats': job['repeats'],
            'strategy': job['strategy'],
            'strategy_label': job['strategy_label'],
            'completed': job['completed'],
            'runs': runs,
            'summary': summary,
            'pagespeed_note': note,
            'probe_note': PROBE_NOTE,
        }

    def _execute(self, job_id):
        try:
            with self._client() as client:
                repeats = self._jobs[job_id]['repeats']
                for index in range(1, repeats + 1):
                    run = self._measure(client, self._jobs[job_id], index)
                    with self._lock:
                        self._jobs[job_id]['runs'].append(run)
                        self._jobs[job_id]['completed'] = index
            with self._lock:
                if self._jobs[job_id]['status'] == 'running':
                    self._jobs[job_id]['status'] = 'done'
        except Exception as error:
            LOGGER.error('speedcheck failed (%s)', type(error).__name__)
            with self._lock:
                job = self._jobs.get(job_id)
                if job and job['status'] == 'running':
                    job['status'] = 'error'
                    job['error'] = '检测中断'

    def _client(self):
        return httpx.Client(
            transport=self.transport,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            headers={'User-Agent': 'ab-lab/speedcheck', 'Accept': 'application/json'},
        )

    def _measure(self, client, job, index):
        probe, probe_error = self._probe(client, job)
        pagespeed, pagespeed_error = self._pagespeed(client, job)
        return {'index': index, 'pagespeed': pagespeed, 'pagespeed_error': pagespeed_error, 'probe': probe, 'probe_error': probe_error}

    def _probe(self, client, job):
        request = {'method': 'GET', 'path': job['path']}
        if job['query']:
            request['query'] = job['query']
        options = {'protocol': job['protocol'], 'ipVersion': 4, 'request': request}
        if job['port']:
            options['port'] = job['port']
        body = {
            'type': 'http',
            'target': job['host'],
            'locations': [{'country': job['region'], 'limit': 1}],
            'measurementOptions': options,
        }
        try:
            response = client.post('https://api.globalping.io/v1/measurements', json=body)
        except httpx.HTTPError:
            return None, '探测请求失败'
        if response.status_code == 429:
            return None, '探测次数太多，请稍后再试'
        if response.status_code == 422 or response.status_code == 404:
            return None, f'{job["region_label"]}暂时没有可用探测点'
        if response.status_code not in (200, 202):
            return None, '探测没有开始'
        try:
            created = response.json()
        except ValueError:
            return None, '探测没有开始'
        if not isinstance(created, dict):
            return None, '探测没有开始'
        measurement_id = created.get('id')
        if created.get('probesCount') == 0 or not isinstance(measurement_id, str) or not _MEASUREMENT.fullmatch(measurement_id):
            return None, f'{job["region_label"]}暂时没有可用探测点'
        for _ in range(30):
            try:
                current = client.get(f'https://api.globalping.io/v1/measurements/{measurement_id}')
                payload = current.json() if current.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                payload = None
            results = payload.get('results') if isinstance(payload, dict) else None
            first = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
            state = first.get('result', {}).get('status') if isinstance(first, dict) and isinstance(first.get('result'), dict) else ''
            if state in ('finished', 'failed'):
                return parse_probe(payload, job['region'], job['region_label'])
            self.sleeper(1)
        return None, '探测超时，没有返回结果'

    def _pagespeed(self, client, job):
        parsed, error, fallback = self._pagespeed_api(client, job)
        if parsed is not None:
            parsed['source'] = 'google'
            parsed['source_label'] = 'Google 实验室'
            return parsed, None
        if fallback:
            local, local_error = self._lighthouse_score(job)
            if local is not None:
                return local, None
            if error == QUOTA_ERROR and not local_error:
                return None, QUOTA_ERROR
            if local_error:
                return None, local_error
        return None, error

    def _pagespeed_api(self, client, job):
        params = {'url': job['url'], 'strategy': job['strategy'], 'category': 'performance'}
        key = os.environ.get('AB_PAGESPEED_API_KEY', '').strip()
        if key:
            params['key'] = key
        try:
            response = client.get('https://www.googleapis.com/pagespeedonline/v5/runPagespeed', params=params, timeout=65)
        except httpx.HTTPError:
            return None, 'PageSpeed 请求失败', True
        if response.status_code == 429:
            return None, QUOTA_ERROR, True
        if response.status_code >= 500:
            return None, 'PageSpeed 没有返回评分', True
        if response.status_code != 200:
            return None, 'PageSpeed 没有返回评分', False
        try:
            return parse_pagespeed(response.json()), None, False
        except (ValueError, TypeError):
            return None, 'PageSpeed 没有返回评分', False

    def _lighthouse_score(self, job):
        if self.lighthouse is False:
            return None, None
        if callable(self.lighthouse):
            payload, error = self.lighthouse(job)
            if payload is None:
                return None, error
            try:
                parsed = parse_pagespeed(payload)
            except (ValueError, TypeError):
                return None, 'Lighthouse 没有返回评分'
            parsed['source'] = 'lighthouse'
            parsed['source_label'] = '本机 Lighthouse'
            return parsed, None
        return self._run_lighthouse(job)

    def _run_lighthouse(self, job):
        binary = _lighthouse_binary()
        chrome = _chrome_binary()
        if not binary or not chrome or not host_is_public(job['host']):
            return None, None
        proxy = _PublicProxy()
        work = tempfile.mkdtemp(prefix='ab-speed-')
        flags = ' '.join((
            '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
            f'--user-data-dir={work}/chrome', f'--proxy-server=http://127.0.0.1:{proxy.port}',
            '--proxy-bypass-list=<-loopback>',
        ))
        command = [
            binary, f'--chrome-flags={flags}',
            '--only-categories=performance', '--output=json', '--output-path=stdout', '--quiet',
        ]
        if job['strategy'] == 'desktop':
            command.append('--preset=desktop')
        else:
            command.append('--form-factor=mobile')
        command.append(job['url'])
        env = _clean_env()
        env['CHROME_PATH'] = chrome
        try:
            process = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                stdout, _stderr = process.communicate(timeout=110)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.communicate()
                return None, 'Lighthouse 评分超时'
        except OSError:
            return None, 'Lighthouse 没有返回评分'
        finally:
            proxy.close()
            shutil.rmtree(work, ignore_errors=True)
        text = stdout.decode('utf-8', 'replace')
        start = text.find('{')
        if start < 0:
            return None, 'Lighthouse 没有返回评分'
        try:
            parsed = parse_pagespeed(json.loads(text[start:]))
        except (ValueError, TypeError):
            return None, 'Lighthouse 没有返回评分'
        parsed['source'] = 'lighthouse'
        parsed['source_label'] = '本机 Lighthouse'
        return parsed, None
