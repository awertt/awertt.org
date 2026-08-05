#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

REPO_URL="https://github.com/awertt/awertt.org.git"
APP_ROOT="/opt/awertt-dci"
REPO_DIR="$APP_ROOT/repo"
APP_DIR="$REPO_DIR/apps/dci-lab"
DATA_DIR="/var/lib/awertt-dci"
ENV_FILE="/etc/awertt-dci.env"
SERVICE_FILE="/etc/systemd/system/awertt-dci.service"
NGINX_SITE="/etc/nginx/sites-enabled/awertt.org"
NGINX_SNIPPET="/etc/nginx/snippets/awertt-dci.conf"
DB_FILE="$DATA_DIR/dci_scores_master.sqlite"
DB_DRIVE_ID="1k9_vGEAdvDMN1xUa8tqFCHb5s66ynJpG"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git nginx python3 python3-venv python3-pip curl ca-certificates

if ! id dciweb >/dev/null 2>&1; then
  useradd --system --home "$APP_ROOT" --shell /usr/sbin/nologin dciweb
fi
mkdir -p "$APP_ROOT" "$DATA_DIR"

if [[ -d "$REPO_DIR/.git" ]]; then
  git -C "$REPO_DIR" fetch --prune origin
  git -C "$REPO_DIR" reset --hard origin/main
else
  rm -rf "$REPO_DIR"
  git clone --depth 1 "$REPO_URL" "$REPO_DIR"
fi

python3 -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/pip" install --upgrade pip wheel
"$APP_ROOT/venv/bin/pip" install -r "$APP_DIR/requirements.txt" gdown

if [[ ! -s "$DB_FILE" ]]; then
  echo "Downloading the DCI SQLite database from Google Drive..."
  rm -f "$DB_FILE"
  "$APP_ROOT/venv/bin/gdown" "$DB_DRIVE_ID" -O "$DB_FILE"
fi

python3 - "$DB_FILE" <<'PY'
import sqlite3, sys
path=sys.argv[1]
con=sqlite3.connect(path)
check=con.execute('PRAGMA integrity_check').fetchone()[0]
count=con.execute('SELECT COUNT(*) FROM v_performance_summary').fetchone()[0]
con.close()
if check != 'ok' or count < 6000:
    raise SystemExit(f'Database validation failed: integrity={check!r}, performances={count}')
print(f'Database validated: {count} appearances, integrity {check}')
PY

if [[ -f "$ENV_FILE" ]]; then
  SQL_PASSWORD="$(sed -n 's/^DCI_SQL_PASSWORD=//p' "$ENV_FILE" | head -1)"
fi
if [[ -z "${SQL_PASSWORD:-}" ]]; then
  SQL_PASSWORD="$(openssl rand -base64 24 | tr -d '=+/\n' | cut -c1-28)"
fi
cat > "$ENV_FILE" <<EOF
DCI_DB_PATH=$DB_FILE
DCI_SQL_PASSWORD=$SQL_PASSWORD
PORT=3001
EOF
chmod 600 "$ENV_FILE"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=awertt.org DCI Data Lab
After=network.target

[Service]
Type=simple
User=dciweb
Group=dciweb
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_ROOT/venv/bin/gunicorn --workers 2 --threads 4 --timeout 60 --bind 127.0.0.1:3001 app:app
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=$REPO_DIR $DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

cat > "$NGINX_SNIPPET" <<'EOF'
location = /dci {
    return 301 /dci/;
}

location /dci/ {
    proxy_pass http://127.0.0.1:3001/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /dci;
    proxy_read_timeout 60s;
}
EOF

if [[ ! -f "$NGINX_SITE" ]]; then
  echo "Nginx site file not found: $NGINX_SITE" >&2
  exit 1
fi

python3 - "$NGINX_SITE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()
include='  include /etc/nginx/snippets/awertt-dci.conf;\n\n'
if 'awertt-dci.conf' not in s:
    marker='  location / {'
    if marker not in s:
        raise SystemExit('Could not find the main location block in the awertt.org Nginx file.')
    s=s.replace(marker, include+marker, 1)
    p.write_text(s)
PY

chown -R root:root "$REPO_DIR"
chown -R dciweb:dciweb "$DATA_DIR"
chmod 750 "$DATA_DIR"
chmod 640 "$DB_FILE"

systemctl daemon-reload
systemctl enable --now awertt-dci.service
nginx -t
systemctl reload nginx

sleep 2
curl -fsS http://127.0.0.1:3001/health >/tmp/awertt-dci-health.json
cat /tmp/awertt-dci-health.json

echo
echo "DCI Data Lab installed."
echo "Public site: https://awertt.org/dci/"
echo "Colts page: https://awertt.org/dci/corps/Colts"
echo "Head-to-head: https://awertt.org/dci/head-to-head?a=Colts&b=Troopers"
echo "Protected SQL: https://awertt.org/dci/sql"
echo "SQL username: anything"
echo "SQL password: $SQL_PASSWORD"
echo
echo "Save the SQL password somewhere private. It is also stored in $ENV_FILE."
