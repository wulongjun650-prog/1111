"""Narrow aaPanel v2 site API, not a general-purpose panel command gateway.

create_static is an internal primitive: the local adapter must also verify
filesystem ownership before calling it. Writes default off; no CLI enables them.
"""
import json
import re
from urllib.parse import urlsplit

from .provisioning import PanelPreflight, ProvisioningError
from .sites import normalize_domain


def validate_identity(identity):
    if not isinstance(identity, dict) or set(identity) != {'site_id', 'domain', 'path', 'owner'}:
        raise ValueError('站点身份字段不完整')
    for key in ('site_id', 'owner'):
        if not isinstance(identity[key], str) or not re.fullmatch('[a-f0-9]{32}', identity[key]):
            raise ValueError('站点身份格式无效')
    if (normalize_domain(identity['domain']) != identity['domain']
            or identity['path'] != '/www/wwwroot/ab-lab-sites/' + identity['site_id']):
        raise ValueError('站点域名或路径无效')


class PanelSites(PanelPreflight):
    def __init__(self, address, key, transport=None, *, writes_enabled=False):
        super().__init__(address, key, transport)
        self.writes_enabled = writes_enabled is True

    @classmethod
    def from_local_config(cls, address, path, transport=None, *, writes_enabled=False):
        from .certificates import _read_archive

        def private_reader(source, limit):
            return _read_archive(source, limit, private=True)

        instance = super().from_local_config(
            address, path, transport, reader=private_reader)
        instance.writes_enabled = writes_enabled is True
        return instance

    def _require_writes(self):
        if not self.writes_enabled or urlsplit(self.address).hostname != '127.0.0.1':
            raise ProvisioningError('自动面板写入未启用或不是本机连接')

    def _domains(self, table='domain'):
        field = 'domain' if table == 'binding' else 'name'
        rows = self._request('/v2/data', 'getData', {'table': table, 'list': 'True', 'search': ''})
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ProvisioningError('面板域名列表不完整或超过支持范围')
        normalized = []
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('pid')) is not int or row['pid'] <= 0
                    or not isinstance(row.get(field), str)):
                raise ProvisioningError('面板域名列表格式不兼容')
            name = row[field].lower().rstrip('.')
            try:
                name = ('*.' + normalize_domain(name[2:]) if name.startswith('*.')
                        else normalize_domain(name))
            except ValueError:
                raise ProvisioningError('面板包含无法安全判定的域名绑定，停止自动接入') from None
            normalized.append(dict(row, name=name, subdirectory=table == 'binding'))
        return normalized

    def _sites(self, domain):
        rows, seen = [], set()
        # Stop only on an empty page, not a short page: panels may clamp limit.
        for page in range(1, 102):
            message = self._request('/v2/data', 'getData', {
                'table': 'sites', 'p': page, 'limit': 20, 'search': domain, 'type': '-1',
            })
            batch = message.get('data') if isinstance(message, dict) else None
            if not isinstance(batch, list):
                raise ProvisioningError('面板站点列表格式不兼容')
            if not batch:
                return rows
            for row in batch:
                if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                        or row['id'] in seen or not all(isinstance(row.get(k), str) for k in ('name', 'path', 'ps'))):
                    raise ProvisioningError('面板分页重复或站点数据不完整，停止接入')
                seen.add(row['id'])
                rows.append(row)
        raise ProvisioningError('面板站点查询超过支持范围，停止接入')

    def inspect(self, domain, path):
        if normalize_domain(domain) != domain or not re.fullmatch('/www/wwwroot/ab-lab-sites/[a-f0-9]{32}', path):
            raise ValueError('站点查询域名或路径无效')
        aliases = self._domains() + self._domains('binding')
        matches = []
        for alias in aliases:
            name = alias['name'].lower().rstrip('.')
            if name == domain or (name.startswith('*.') and domain.endswith(name[1:])):
                matches.append(alias)
        if any(row['subdirectory'] for row in matches):
            raise ProvisioningError('域名已绑定到站点子目录，拒绝创建或接管')
        sites = [site for site in self._sites(domain) if site['name'].lower().rstrip('.') == domain]
        if not matches and not sites:
            return None
        if len(sites) != 1 or not matches:
            raise ProvisioningError('域名已被其他站点或别名占用')
        site = sites[0]
        marker = re.fullmatch(r'ab-lab:([a-f0-9]{32}):([a-f0-9]{32})', site['ps'])
        if (site['path'] != path or not marker or marker[1] != path.rsplit('/', 1)[1]
                or any(row['pid'] != site['id'] or row['name'].lower().rstrip('.') != domain for row in matches)
                or any(row['subdirectory'] or row['name'].lower().rstrip('.') != domain
                       for row in aliases if row['pid'] == site['id'])):
            raise ProvisioningError('站点路径、所有权标记或域名绑定不一致，拒绝接管')
        return dict(site_id=marker[1], domain=domain, path=path, owner=marker[2], panel_id=site['id'])

    def create_static(self, identity):
        self._require_writes()
        validate_identity(identity)
        if self.inspect(identity['domain'], identity['path']) is not None:
            raise ProvisioningError('站点已存在，不重复创建')
        result = self._request('/v2/site', 'AddSite', {
            'webname': json.dumps({'domain': identity['domain'], 'domainlist': [], 'count': 0}),
            'type': 'PHP', 'project_type': 'PHP', 'port': '80', 'type_id': '0',
            'ps': 'ab-lab:' + identity['site_id'] + ':' + identity['owner'],
            'path': identity['path'], 'version': '00', 'ftp': 'false', 'sql': 'false',
            'datapassword': '', 'codeing': 'utf8', 'set_ssl': '0', 'force_ssl': '0',
            'ssl_auto': '0', 'is_create_default_file': 'false',
        })
        if (not isinstance(result, dict) or result.get('siteStatus') is not True
                or type(result.get('siteId')) is not int or result['siteId'] <= 0):
            raise ProvisioningError('创建结果不明确，须核对站点归属，不能盲目重试')
        return result['siteId']
