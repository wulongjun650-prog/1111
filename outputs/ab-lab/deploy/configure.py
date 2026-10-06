"""Generate only AB Lab's files. Never edit a BaoTa site or global Nginx config."""
import argparse
import getpass
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ablab.auth import Auth
from ablab.settings import Deployment
from ablab.store import Store


def generate(destination, admin_domain, target_domain):
    settings = Deployment(admin_origin=f'https://{admin_domain}', target_origin=f'https://{target_domain}')
    destination = Path(destination)
    files = {'config.json': json.dumps({
        'admin_origin': settings.admin_origin, 'target_origin': settings.target_origin,
        'data_dir': '/var/lib/ab-lab', 'geoip_path': '', 'server_ip': '',
    }, indent=2) + '\n'}
    for service, port in [('admin', 8765), ('target', 8766)]:
        files[f'ab-lab-{service}.service'] = f'''[Unit]
Description=AB Lab {service}
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=ab-lab
Group=ab-lab
WorkingDirectory=/opt/ab-lab
ExecStart=/opt/ab-lab/.venv/bin/python /opt/ab-lab/run.py --service {service} --config /etc/ab-lab/config.json
Restart=on-failure
RestartSec=5
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/ab-lab
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
'''
        domain = admin_domain if service == 'admin' else target_domain
        files[f'nginx-{service}.conf'] = f'''# Paste INSIDE only the new {domain} HTTPS server block.
# Remove its old location / or old reverse-proxy entries first.
# Keep BaoTa certificate and ACME renewal configuration. No global edits.
client_max_body_size 20m;
location ^~ / {{
    proxy_pass http://127.0.0.1:{port};
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host {domain};
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For "";
    proxy_set_header Forwarded "";
    proxy_cache off;
    proxy_buffering off;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
    proxy_intercept_errors off;
}}
'''
    destination.mkdir(parents=True, exist_ok=True)
    for name in files:
        if (destination / name).exists():
            raise FileExistsError(f'拒绝覆盖已有配置：{destination / name}')
    for name, content in files.items():
        with (destination / name).open('x', encoding='utf-8', newline='\n') as output:
            output.write(content)


def set_account(data_dir):
    username = input('管理员账号（字母、数字、_.@-）：').strip()
    password = getpass.getpass('密码（至少 12 字符，不会显示）：')
    confirm = getpass.getpass('再输入一次密码：')
    if password != confirm:
        raise ValueError('两次密码不一致，未设置')
    Auth(Store(data_dir)).set_password(username, password)
    print('账号已设置，旧会话已失效。')


def main():
    parser = argparse.ArgumentParser(description='AB Lab deployment configuration')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--admin-domain')
    parser.add_argument('--target-domain')
    parser.add_argument('--set-password', action='store_true')
    parser.add_argument('--data', type=Path, default=Path('/var/lib/ab-lab'))
    args = parser.parse_args()
    if args.output:
        admin = args.admin_domain or input('后台域名（不带 https://）：').strip().lower()
        target = args.target_domain or input('访客域名（不带 https://）：').strip().lower()
        generate(args.output, admin, target)
    if args.set_password:
        set_account(args.data)
    if not args.output and not args.set_password:
        parser.error('请选择 --output 或 --set-password')


if __name__ == '__main__':
    main()
