"""Cached Google Web Risk Lookup checks for registered domain root URLs."""
from datetime import datetime
import json
import secrets
import time

import httpx

from .sites import normalize_domain


ENDPOINT = 'https://webrisk.googleapis.com/v1/uris:search'
THREATS = ('MALWARE', 'SOCIAL_ENGINEERING', 'UNWANTED_SOFTWARE')
FIELDS = ('status', 'checked_at', 'expires_at', 'threats', 'detail')


class GoogleReputation:
    def __init__(self, registry, key='', transport=None):
        self.registry, self.key, self.transport = registry, key, transport
        with registry.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS google_reputation(
                    site_id TEXT PRIMARY KEY, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS google_reputation_locks(
                    site_id TEXT PRIMARY KEY, token TEXT NOT NULL, until REAL NOT NULL);
            ''')

    @property
    def configured(self):
        return bool(self.key)

    @staticmethod
    def _result(status, checked_at=None, expires_at=None, threats=None, detail=''):
        return dict(status=status, checked_at=checked_at, expires_at=expires_at,
                    threats=threats or [], detail=detail)

    def _present(self, site, cached, now):
        try:
            normalize_domain(site['domain'])
        except ValueError:
            return self._result('unavailable', detail='此站点没有可检测的公网域名')
        if not self.configured:
            return self._result('unconfigured', detail='Google Web Risk API 密钥未配置')
        if cached is None:
            return self._result('unchecked', detail='尚未检测')
        result = {field: cached[field] for field in FIELDS}
        if result['status'] == 'flagged':
            fresh = [match for match in cached['matches'] if match['expires_at'] > now]
            if fresh:
                result['threats'] = [kind for kind in THREATS if any(kind in match['threats'] for match in fresh)]
                result['expires_at'] = max(match['expires_at'] for match in fresh)
                return result
        if result['status'] in ('clean', 'flagged') and result['expires_at'] <= now:
            return self._result('expired', result['checked_at'], result['expires_at'],
                                detail='检测结果已过期，请重新检测')
        return result

    def get(self, site_id):
        site = self.registry.get(site_id)
        with self.registry.connect() as db:
            row = db.execute('SELECT result FROM google_reputation WHERE site_id=?', (site_id,)).fetchone()
        return self._present(site, json.loads(row['result']) if row else None, time.time())

    def catalog(self, sites):
        with self.registry.connect() as db:
            cached = {row['site_id']: json.loads(row['result']) for row in db.execute(
                'SELECT site_id,result FROM google_reputation')}
        now = time.time()
        for site in sites:
            site['google_reputation'] = self._present(site, cached.get(site['id']), now)
        return sites

    @staticmethod
    def _fresh(cached, now):
        return cached and (cached.get('expires_at') or cached.get('retry_after', 0)) > now

    def _lookup(self, client, uri, now):
        params = [('uri', uri), *(("threatTypes", threat) for threat in THREATS)]
        try:
            with client.stream('GET', ENDPOINT, params=params,
                               headers={'x-goog-api-key': self.key}) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 64 * 1024:
                        raise ValueError('oversized')
                    chunks.append(chunk)
            data = json.loads(b''.join(chunks))
            if data == {} or data == {'threat': {}}:
                return [], None
            if not isinstance(data, dict) or not isinstance(data.get('threat'), dict):
                raise ValueError('invalid response')
            threat = data['threat']
            kinds = threat.get('threatTypes')
            if not isinstance(kinds, list) or not kinds or any(kind not in THREATS for kind in kinds):
                raise ValueError('invalid threat types')
            stamp = datetime.fromisoformat(threat['expireTime'].replace('Z', '+00:00'))
            expiry = stamp.timestamp()
            if stamp.tzinfo is None or expiry <= now:
                raise ValueError('expired threat')
            return kinds, expiry
        except (httpx.HTTPError, ValueError, TypeError, KeyError, UnicodeError):
            return None, None

    def check(self, site_id):
        site = self.registry.get(site_id)
        initial = self.get(site_id)
        if initial['status'] in ('unavailable', 'unconfigured'):
            return initial
        now = time.time()
        token = secrets.token_hex(12)
        with self.registry.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT result FROM google_reputation WHERE site_id=?', (site_id,)).fetchone()
            cached = json.loads(row['result']) if row else None
            if self._fresh(cached, now):
                return self._present(site, cached, now)
            lock = db.execute('SELECT until FROM google_reputation_locks WHERE site_id=?', (site_id,)).fetchone()
            if lock and lock['until'] > now:
                return self._present(site, cached, now)
            db.execute('INSERT OR REPLACE INTO google_reputation_locks VALUES(?,?,?)',
                       (site_id, token, now + 30))
        try:
            domain = normalize_domain(site['domain'])
            with httpx.Client(transport=self.transport, timeout=5, follow_redirects=False, trust_env=False) as client:
                results = [self._lookup(client, f'{scheme}://{domain}/', now) for scheme in ('https', 'http')]
            threats = [kind for kind in THREATS if any(result[0] and kind in result[0] for result in results)]
            if threats:
                result = self._result('flagged', now, max(expiry for kinds, expiry in results if kinds),
                                      threats, 'Google Web Risk 检出风险')
                result['matches'] = [{'threats': kinds, 'expires_at': expiry}
                                     for kinds, expiry in results if kinds]
            elif any(kinds is None for kinds, _ in results):
                result = self._result('error', now, detail='Google Web Risk 检测未完成，请稍后重试')
                result['retry_after'] = now + 60
            else:
                result = self._result('clean', now, now + 300,
                                      detail='本次查询未命中指定风险列表；结果不是持续安全保证')
            with self.registry.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT token FROM google_reputation_locks WHERE site_id=?', (site_id,)).fetchone()
                if current and current['token'] == token:
                    db.execute('INSERT OR REPLACE INTO google_reputation VALUES(?,?)',
                               (site_id, json.dumps(result, ensure_ascii=False)))
                    db.execute('DELETE FROM google_reputation_locks WHERE site_id=?', (site_id,))
            return self.get(site_id)
        finally:
            with self.registry.connect() as db:
                db.execute('DELETE FROM google_reputation_locks WHERE site_id=? AND token=?', (site_id, token))
