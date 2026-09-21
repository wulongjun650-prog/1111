"""One read-only API call; local config mode never exports the panel key."""
import argparse
import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ablab.provisioning import PanelPreflight


def main(argv=None):
    parser = argparse.ArgumentParser(description='面板只读接口检查，不创建站点或修改配置')
    parser.add_argument('--address', help='面板根地址；本机配置模式仅允许 127.0.0.1')
    parser.add_argument('--local-config', action='store_true', help='在服务器读取现有面板配置，不提示、不导出密钥')
    args = parser.parse_args(argv)
    address = args.address or input('面板根地址（远程必须 HTTPS）：').strip()
    try:
        if args.local_config:
            preflight = PanelPreflight.from_local_config(address)
        else:
            key = getpass.getpass('API 密钥（不显示、不保存）：')
            preflight = PanelPreflight(address, key)
        result = preflight.check()
        print(result['detail'])
    except ValueError as error:
        print(str(error))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
