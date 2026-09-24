"""Recoverable provisioning coordinator, not yet connected to the live CLI.

The privileged adapter must reject foreign paths/aliases in inspect(), check
ownership again at each write, and make configure() idempotent. Certificate
submission is attempted at most once; uncertain orders are only verified.
The web process must not have access to state_dir or the adapter's credentials.
"""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .provisioning import DNSChecker, ProvisioningError, ProvisioningNotStarted, public_ipv4
from .sites import normalize_domain


@contextmanager
def worker_lock(path):
    # ponytail: one process lock serializes all domains on this small server.
    # Introduce per-site locks only if provisioning throughput requires it.
    with path.open('a+b') as handle:
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            yield False
            return
        try:
            yield True
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProvisioningWorker:
    def __init__(self, registry, state_dir, server_ip, panel, checker=None):
        state_dir = Path(state_dir)
        if state_dir.is_symlink():
            raise ValueError('建站任务目录不能是符号链接')
        self.root = state_dir.resolve()
        if self.root.is_relative_to(registry.root) or self.root.is_relative_to(registry.path.parent):
            raise ValueError('建站任务记录必须与网页可写数据目录分离')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            info = self.root.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('建站任务目录必须仅允许当前服务账号访问（0700）')
        self.path = self.root / 'jobs.db'
        self.lock_path = self.root / 'worker.lock'
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ValueError('建站任务文件不能是符号链接')
        self.registry, self.panel = registry, panel
        self.server_ip, self.checker = public_ipv4(server_ip), checker or DNSChecker()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs(
                site_id TEXT PRIMARY KEY, domain TEXT NOT NULL, path TEXT NOT NULL,
                owner TEXT NOT NULL, phase TEXT NOT NULL, panel_id INTEGER)''')

    def _load(self, site_id):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM jobs WHERE site_id=?', (site_id,)).fetchone()
            return dict(row) if row else None

    def _save(self, job):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''INSERT INTO jobs(site_id,domain,path,owner,phase,panel_id)
                VALUES(:site_id,:domain,:path,:owner,:phase,:panel_id)
                ON CONFLICT(site_id) DO UPDATE SET phase=excluded.phase,panel_id=excluded.panel_id''', job)

    def _owned(self, identity, panel_id=None):
        remote = self.panel.inspect(identity['domain'], identity['path'])
        if (not isinstance(remote, dict)
                or any(remote.get(key) != value for key, value in identity.items())
                or type(remote.get('panel_id')) is not int or remote['panel_id'] <= 0
                or (panel_id is not None and remote['panel_id'] != panel_id)):
            raise ProvisioningError('站点归属不一致或创建结果不明确；需人工核对，不会覆盖或重复创建')
        return remote['panel_id']

    def _current(self, site):
        latest = self.registry.get(site['id'])
        return (latest['enabled'] and latest['generation'] == site['generation']
                and latest['domain'] == site['domain'])

    def _stage(self, site, stage, **kwargs):
        return self._current(site) and self.registry.transition(site['id'], site['generation'], stage, **kwargs)

    def run_once(self, site_id):
        with worker_lock(self.lock_path) as acquired:
            if not acquired:
                return 'busy'
            site = self.registry.get(site_id)
            if (site_id == 'default' or not site['enabled'] or site['stage'] in ('active', 'paused')
                    or site['next_attempt'] > time.time()):
                return 'skipped'
            try:
                if not re.fullmatch('[a-f0-9]{32}', site_id) or normalize_domain(site['domain']) != site['domain']:
                    raise ProvisioningError('站点身份记录无效')
                try:
                    self.checker.check(site['domain'], self.server_ip)
                except Exception:
                    self._stage(site, 'waiting_dns', detail='DNS 检查未通过；未执行面板写入', delay=300)
                    return 'waiting_dns'
                if not self._current(site):
                    return 'paused'
                self._process(site)
            except Exception:
                # Never persist the adapter exception text or a raw panel response.
                self._stage(site, 'failed', detail='接入未完成：请核对站点归属、创建结果和证书状态；已有数据保留，不会盲目重建或重复申请证书', delay=3600)
            return self.registry.get(site_id)['stage']

    def renew_once(self, site_id):
        with worker_lock(self.lock_path) as acquired:
            if not acquired:
                return 'busy'
            site = self.registry.get(site_id)
            if site_id == 'default' or not site['enabled'] or site['stage'] != 'active':
                return 'skipped'
            if not re.fullmatch('[a-f0-9]{32}', site_id) or normalize_domain(site['domain']) != site['domain']:
                raise ProvisioningError('续期站点身份记录无效')
            job = self._load(site_id)
            path = '/www/wwwroot/ab-lab-sites/' + site_id
            if (job is None or job['phase'] != 'verified' or job['domain'] != site['domain']
                    or job['path'] != path or type(job['panel_id']) is not int
                    or job['panel_id'] <= 0 or site['panel_id'] != job['panel_id']
                    or site['managed_path'] != path):
                raise ProvisioningError('续期任务缺少已验收的建站记录')
            identity = {key: job[key] for key in ('site_id', 'domain', 'path', 'owner')}
            self._owned(identity, job['panel_id'])
            proof = self.panel.renew(
                identity, job['panel_id'], lambda: self._current(site))
            if (not isinstance(proof, dict)
                    or set(proof) != {'https', 'route', 'renewal'}
                    or any(proof[key] is not True for key in proof)):
                raise ProvisioningError('生产续期后的 HTTPS、入口路由或演练未全部验证')
            self._owned(identity, job['panel_id'])
            return 'renewed'

    def _process(self, site):
        job = self._load(site['id'])
        path = '/www/wwwroot/ab-lab-sites/' + site['id']
        if job is None:
            if self.panel.inspect(site['domain'], path) is not None:
                raise ProvisioningError('域名或受管路径已存在，不自动接管')
            if not self._stage(site, 'creating'):
                return
            job = dict(site_id=site['id'], domain=site['domain'], path=path,
                       owner=uuid.uuid4().hex, phase='create_intent', panel_id=None)
            # Commit before the external mutation. Crash/timeout then reconciles,
            # including the conservative case where no remote object is found.
            self._save(job)
            if not self._current(site):
                # Orderly cancellation is known not to have reached create().
                # Only a crash/exception leaves an uncertain durable intent.
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("DELETE FROM jobs WHERE site_id=? AND owner=? AND phase='create_intent' AND panel_id IS NULL",
                               (job['site_id'], job['owner']))
                return
            identity = {key: job[key] for key in ('site_id', 'domain', 'path', 'owner')}
            self.panel.create(identity)
        else:
            if (job['domain'] != site['domain'] or job['path'] != path
                    or job['phase'] not in ('create_intent', 'created', 'configured', 'certificate_intent', 'verified')):
                raise ProvisioningError('持久化建站记录不一致，停止接入')
            identity = {key: job[key] for key in ('site_id', 'domain', 'path', 'owner')}

        job['panel_id'] = self._owned(identity, job['panel_id'])
        if job['phase'] == 'create_intent':
            job['phase'] = 'created'
            self._save(job)
        if job['phase'] == 'created':
            self._owned(identity, job['panel_id'])
            if not self._stage(site, 'proxy', panel_id=job['panel_id'], managed_path=path):
                return
            self.panel.configure(identity, job['panel_id'])
            job['phase'] = 'configured'
            self._save(job)
        if job['phase'] == 'configured':
            self._owned(identity, job['panel_id'])
            if not self._stage(site, 'certificate', panel_id=job['panel_id'], managed_path=path):
                return
            job['phase'] = 'certificate_intent'
            self._save(job)
            if not self._current(site):
                job['phase'] = 'configured'
                self._save(job)
                return
            try:
                self.panel.certificate(identity, job['panel_id'], lambda: self._current(site))
            except ProvisioningNotStarted:
                # The adapter guarantees no external request was launched, so
                # this durable intent can safely be retried after the gate is fixed.
                job['phase'] = 'configured'
                self._save(job)
                raise

        self._owned(identity, job['panel_id'])
        if not self._stage(site, 'verifying', panel_id=job['panel_id'], managed_path=path):
            return
        proof = self.panel.verify(identity, job['panel_id'], lambda: self._current(site))
        if not isinstance(proof, dict) or any(proof.get(key) is not True for key in ('https', 'route', 'renewal')):
            raise ProvisioningError('HTTPS、入口路由或续期未全部验证')
        self._owned(identity, job['panel_id'])
        job['phase'] = 'verified'
        self._save(job)
        self._stage(site, 'active', detail='HTTPS、入口路由与续期验证通过', panel_id=job['panel_id'], managed_path=path)
