"""Loopback services: local demo or explicit HTTPS reverse-proxy deployment."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description='AB Lab local console')
    parser.add_argument('--service', choices=['admin', 'target', 'dns', 'origin', 'provision', 'renew'])
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parent / 'data')
    parser.add_argument('--config', type=Path, help='Public deployment JSON; requires --service')
    parser.add_argument('--private-config', type=Path, help='Root-only provisioning JSON')
    args = parser.parse_args()
    if args.service == 'origin' and (args.private_config or not args.config):
        parser.error('origin 必须提供 --config，不能使用 --private-config')
    if args.service in ('provision', 'renew'):
        if args.config or not args.private_config:
            parser.error('provision/renew 只接受并且必须提供 --private-config')
        from ablab.privileged_worker import load_worker, run_pending, run_renewals
        worker = load_worker(args.private_config)
        if args.service == 'renew':
            print(f'Production renewal checked {run_renewals(worker)} active sites.', flush=True)
            return
        print('Privileged provisioning enabled by private configuration.', flush=True)
        while True:
            run_pending(worker)
            time.sleep(60)
    if args.private_config:
        parser.error('--private-config 只能用于 --service provision/renew')
    deployment = None
    config = {}
    if args.config:
        if not args.service:
            parser.error('--config 必须与 --service 一起使用；正式环境由 systemd 分别托管')
        from ablab.settings import Deployment
        config = json.loads(args.config.read_text(encoding='utf-8'))
        deployment = Deployment(admin_origin=config['admin_origin'], target_origin=config['target_origin'])
        args.data = Path(config['data_dir'])
        if not args.data.is_absolute():
            parser.error('正式环境 data_dir 必须是绝对路径')
        os.environ['AB_GEOIP_PATH'] = config.get('geoip_path', '')
    if args.service == 'dns':
        from ablab.cloudflare import Cloudflare
        from ablab.provisioning import inspect_pending
        from ablab.sites import Registry
        registry = Registry(args.data, deployment.host(False) if deployment else '', registry_dir=config.get('registry_dir'))
        cloudflare = Cloudflare(os.environ.get('AB_CLOUDFLARE_TOKEN', ''), os.environ.get('AB_CLOUDFLARE_TEMPLATE', ''), config.get('server_ip', ''))
        print('DNS inspection only. Site creation runs in the origin service.', flush=True)
        while True:
            inspect_pending(registry, config.get('server_ip', ''), cloudflare=cloudflare if cloudflare.configured else None)
            time.sleep(60)
    if args.service == 'origin':
        from ablab.origin_setup import serve_origin
        serve_origin(args.data, config.get('server_ip', ''))
        return
    if args.service:
        import uvicorn
        from ablab.web import create_admin, create_target
        factory, port = (create_admin, 8765) if args.service == 'admin' else (create_target, 8766)
        options = {'deployment': deployment, 'registry_dir': config.get('registry_dir')}
        if args.service == 'admin':
            options['server_ip'] = config.get('server_ip', '')
        uvicorn.run(factory(args.data, **options), host='127.0.0.1', port=port, proxy_headers=False, access_log=False, limit_concurrency=40, timeout_keep_alive=5)
        return
    children = []
    try:
        for service in ('admin', 'target'):
            children.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--service', service, '--data', str(args.data.resolve())]))
        print('AB Lab console: http://127.0.0.1:8765\nTarget: http://127.0.0.1:8766\nLocal only. Press Ctrl+C to stop.', flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == '__main__':
    main()
