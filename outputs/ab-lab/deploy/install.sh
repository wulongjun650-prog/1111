#!/usr/bin/env bash
# First install only. Does not modify BaoTa, firewall, certificates or Nginx.
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 ]]; then
    echo 'Please run: sudo bash deploy/install.sh' >&2
    exit 1
fi
if [[ ! -d /run/systemd/system ]]; then
    echo 'A running systemd Linux host is required.' >&2
    exit 1
fi
source /etc/os-release
if [[ ${ID:-} != ubuntu ]]; then
    echo 'This installer targets Ubuntu. See the deployment guide.' >&2
    exit 1
fi
python3 -c 'import sys, venv; assert sys.version_info >= (3,12), "Python 3.12+ required"'
command -v runuser >/dev/null
command -v useradd >/dev/null
for destination in /opt/ab-lab /etc/ab-lab /var/lib/ab-lab /etc/systemd/system/ab-lab-admin.service /etc/systemd/system/ab-lab-target.service; do
    if [[ -e "$destination" || -L "$destination" ]]; then
        echo "Refusing to overwrite existing path: $destination" >&2
        exit 1
    fi
done
if getent passwd ab-lab >/dev/null || getent group ab-lab >/dev/null; then
    echo 'The ab-lab user/group already exists. Review the earlier installation manually.' >&2
    exit 1
fi
python3 - <<'PY'
import socket
for port in (8765, 8766):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
PY

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
for required in run.py requirements.txt ablab templates static deploy resources; do
    [[ -e "$source_dir/$required" ]] || { echo "Missing source: $required" >&2; exit 1; }
done
echo 'Installing only into /opt/ab-lab, /etc/ab-lab, /var/lib/ab-lab.'
echo 'Existing sites and Nginx will NOT be changed. No automatic public exposure.'
trap 'echo "Installation interrupted. No existing files were overwritten. Preserve the new AB Lab directories and inspect them before retrying; do not delete existing site data." >&2' ERR
install -d -m 0755 /opt/ab-lab
for item in run.py requirements.txt ablab templates static deploy demo resources README.md; do
    cp -R -- "$source_dir/$item" /opt/ab-lab/
done
python3 -m venv /opt/ab-lab/.venv
/opt/ab-lab/.venv/bin/python -m pip install -r /opt/ab-lab/requirements.txt
# venv/pip inherit the private umask too; service user must read/execute code.
chmod -R u=rwX,go=rX /opt/ab-lab
useradd --system --user-group --home-dir /var/lib/ab-lab --no-create-home --shell /usr/sbin/nologin ab-lab
install -d -o root -g ab-lab -m 0750 /etc/ab-lab
install -d -o ab-lab -g ab-lab -m 0700 /var/lib/ab-lab
/opt/ab-lab/.venv/bin/python /opt/ab-lab/deploy/configure.py --output /etc/ab-lab
chown root:ab-lab /etc/ab-lab/config.json
chmod 0640 /etc/ab-lab/config.json
runuser -u ab-lab -- /opt/ab-lab/.venv/bin/python /opt/ab-lab/deploy/configure.py --set-password
install -o root -g root -m 0644 /etc/ab-lab/ab-lab-admin.service /etc/systemd/system/ab-lab-admin.service
install -o root -g root -m 0644 /etc/ab-lab/ab-lab-target.service /etc/systemd/system/ab-lab-target.service
systemctl daemon-reload
systemctl enable --now ab-lab-admin ab-lab-target
systemctl is-active --quiet ab-lab-admin ab-lab-target
echo 'Services started on loopback only. Finish HTTPS and proxy setup in deploy/BAOTA.md.'
echo 'Proxy fragments: /etc/ab-lab/nginx-admin.conf and /etc/ab-lab/nginx-target.conf'
