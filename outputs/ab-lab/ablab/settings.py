"""Explicit public deployment settings; never infer trust from request headers."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Deployment:
    admin_origin: str
    target_origin: str

    def __post_init__(self):
        for origin in (self.admin_origin, self.target_origin):
            url = urlsplit(origin)
            if (url.scheme != 'https' or not url.hostname or url.path or url.query or url.fragment
                    or url.username or url.password or url.port not in (None, 443)
                    or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', url.hostname)
                    or '..' in url.hostname or origin != f'https://{url.hostname}'):
                raise ValueError('公网地址必须是不同域名的 HTTPS 根地址，不带路径或端口')
        if urlsplit(self.admin_origin).hostname == urlsplit(self.target_origin).hostname:
            raise ValueError('管理后台和访客站点必须使用不同域名')

    def host(self, admin):
        return urlsplit(self.admin_origin if admin else self.target_origin).netloc
