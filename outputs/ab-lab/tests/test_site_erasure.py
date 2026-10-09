import json
import subprocess

import pytest

from ablab.provisioning import ProvisioningError
from ablab.site_erasure import CANONICAL_WEBROOT, erase_queued, queue_erasure
from ablab.sites import Registry


SITE = 'a' * 32
DOMAIN = 'gone.example.com'


class Panel:
    def __init__(self, fail=None):
        self.deleted, self.fail = [], fail

    def delete_owned(self, domain, path, panel_id=None):
        if self.fail:
            raise ProvisioningError(self.fail)
        self.deleted.append((domain, path, panel_id))
        return panel_id


def layout(tmp_path, text, *, panel_id=22, zone=''):
    web = tmp_path / 'www'
    root = web / SITE
    root.mkdir(parents=True)
    (root / 'index.html').write_text('page', encoding='utf-8')
    configs = tmp_path / 'vhost'
    configs.mkdir()
    conf = configs / (DOMAIN + '.conf')
    conf.write_text(text, encoding='utf-8')
    other = configs / 'other.example.com.conf'
    other.write_text('server { listen 80; server_name other.example.com; }\n', encoding='utf-8')
    cert = tmp_path / 'certs' / SITE
    cert.mkdir(parents=True)
    (cert / 'fullchain.pem').write_text('cert', encoding='utf-8')
    job = tmp_path / 'jobs'
    job.mkdir()
    (job / (SITE + '.json')).write_text('{}', encoding='utf-8')
    receipt = tmp_path / 'nginx'
    receipt.mkdir()
    (receipt / (SITE + '.json')).write_text('{}', encoding='utf-8')
    data = tmp_path / 'data'
    site_dir = data / 'sites' / SITE
    site_dir.mkdir(parents=True)
    (site_dir / 'app.db').write_text('db', encoding='utf-8')
    record = {
        'site_id': SITE, 'domain': DOMAIN, 'path': root.as_posix(), 'panel_id': panel_id,
        'cf_zone_id': zone, 'attempts': 0, 'nginx_removed': False,
    }
    folder = data / 'erasures'
    folder.mkdir()
    path = folder / (SITE + '.json')
    path.write_text(json.dumps(record), encoding='utf-8')
    calls = []

    def runner(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, '', '')

    return folder, conf, other, root, cert, job, receipt, site_dir, calls, runner


def erase(tmp_path, folder, runner, panel):
    return erase_queued(
        folder, panel=panel, certificate_root=tmp_path / 'certs', config_dir=tmp_path / 'vhost',
        receipt_dir=tmp_path / 'nginx', job_dir=tmp_path / 'jobs', webroot_root=tmp_path / 'www',
        data_root=tmp_path / 'data', runner=runner)


def owned_conf(root):
    return (
        f'# AB Lab managed HTTPS entry {SITE}\n'
        'server {\n'
        f'    listen 443 ssl;\n'
        f'    server_name {DOMAIN};\n'
        f'    root {root};\n'
        '}\n'
    )


def test_queue_uses_the_canonical_web_root_and_ignores_another_path(tmp_path):
    registry = Registry(tmp_path)
    site = registry.add('Gone.Example.com')
    site['managed_path'] = '/tmp/not-this'
    site['panel_id'] = 7
    path = queue_erasure(tmp_path, site)
    record = json.loads(path.read_text(encoding='utf-8'))
    assert record['domain'] == 'gone.example.com'
    assert record['path'] == CANONICAL_WEBROOT + '/' + site['id']
    assert record['panel_id'] == 7
    assert record['path'] != site['managed_path']
    with pytest.raises(ValueError):
        queue_erasure(tmp_path, {'id': 'default', 'domain': 'old.example.com'})


def test_owned_files_disappear_and_another_site_stays(tmp_path):
    root = (tmp_path / 'www' / SITE).as_posix()
    folder, conf, other, web, cert, job, receipt, site_dir, calls, runner = layout(tmp_path, owned_conf(root))
    before = other.read_bytes()
    panel = Panel()
    assert erase(tmp_path, folder, runner, panel) == {'done': 1, 'held': 0}
    assert not conf.exists()
    assert not web.exists() and not cert.exists() and not site_dir.exists()
    assert not (job / (SITE + '.json')).exists()
    assert not (receipt / (SITE + '.json')).exists()
    assert not (folder / (SITE + '.json')).exists()
    assert other.read_bytes() == before
    assert panel.deleted == [(DOMAIN, root, 22)]
    assert calls[0][-1] == '-t' and calls[1][-2:] == ['-s', 'reload']


def test_unmarked_config_and_a_foreign_root_are_left_alone(tmp_path):
    root = (tmp_path / 'www' / SITE).as_posix()
    text = f'server {{\n    server_name {DOMAIN};\n    root {root};\n}}\n'
    folder, conf, other, web, cert, _job, _receipt, _site_dir, calls, runner = layout(tmp_path, text)
    panel = Panel()
    assert erase(tmp_path, folder, runner, panel) == {'done': 1, 'held': 0}
    assert conf.read_text(encoding='utf-8') == text
    assert web.is_dir() and (web / 'index.html').read_text(encoding='utf-8') == 'page'
    assert not cert.exists()
    assert panel.deleted == []
    assert calls == []
    assert other.is_file()


def test_nginx_failure_restores_the_config_and_retries_are_capped(tmp_path):
    root = (tmp_path / 'www' / SITE).as_posix()
    folder, conf, _other, web, _cert, _job, _receipt, _site_dir, calls, _runner = layout(tmp_path, owned_conf(root))
    original = conf.read_text(encoding='utf-8')

    def fail(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1, '', '')

    assert erase(tmp_path, folder, fail, Panel()) == {'done': 0, 'held': 0}
    assert conf.read_text(encoding='utf-8') == original
    assert web.is_dir()
    saved = json.loads((folder / (SITE + '.json')).read_text(encoding='utf-8'))
    assert saved['attempts'] == 1
    saved['attempts'] = 4
    (folder / (SITE + '.json')).write_text(json.dumps(saved), encoding='utf-8')
    assert erase(tmp_path, folder, fail, Panel()) == {'done': 0, 'held': 1}
    assert not (folder / (SITE + '.json')).exists()
    assert (folder / 'held' / (SITE + '.json')).is_file()
    assert conf.read_text(encoding='utf-8') == original
