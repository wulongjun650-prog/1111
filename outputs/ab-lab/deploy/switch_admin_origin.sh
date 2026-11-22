#!/bin/bash
# Run on the server (as root). Points the admin console at baozha98.sbs.
set -euo pipefail
CONF=/etc/ab-lab/config.json
NEW_ADMIN=https://baozha98.sbs
OLD_ADMIN=https://hhucuq.top
TARGET=$(python3 -c "import json;print(json.load(open('$CONF'))['target_origin'])")
python3 - <<PY
import json
from pathlib import Path
path = Path("$CONF")
data = json.loads(path.read_text(encoding='utf-8'))
data['admin_origin'] = "$NEW_ADMIN"
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding='utf-8')
print('admin_origin ->', data['admin_origin'])
print('target_origin', data.get('target_origin'))
PY
# Copy nginx vhost if hhucuq exists and baozha98 does not yet.
OLD_VHOST=/www/server/panel/vhost/nginx/hhucuq.top.conf
NEW_VHOST=/www/server/panel/vhost/nginx/baozha98.sbs.conf
if [ -f "$OLD_VHOST" ] && [ ! -f "$NEW_VHOST" ]; then
  sed "s/hhucuq\\.top/baozha98.sbs/g" "$OLD_VHOST" > "$NEW_VHOST"
  /www/server/nginx/sbin/nginx -t
  systemctl reload nginx || /etc/init.d/nginx reload
fi
systemctl restart ab-lab-admin
systemctl restart ab-lab-target
echo DONE
