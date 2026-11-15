#!/usr/bin/env bash
# =============================================================================
#  Atelier PDM - Raspberry Pi server setup  (install_atelier_server.sh)  2.9.0
#
#  Run on the Pi, in the folder that holds atelier_server.py:
#      sudo bash install_atelier_server.sh
#
#  Sets up:
#    * /srv/atelier/vault      the vault folder, shared on the network as  \\<pi-name>\atelier
#    * /srv/atelier/data       the vault database (owned by the server) and its nightly backups
#    * the Atelier server      a service on port 8770 that starts with the Pi
#    * a Samba user for the share, and (if ufw is installed) firewall rules for the local network
#  Safe to run again: it keeps the existing token, database and files.
# =============================================================================
set -euo pipefail

ROOT=/srv/atelier
VAULT=$ROOT/vault
DATA=$ROOT/data
CONF=/etc/atelier/server.json
SVC_USER=atelier
PORT=8770
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then echo "Run with sudo:  sudo bash $0"; exit 1; fi
if [[ ! -f "$HERE/atelier_server.py" ]]; then echo "atelier_server.py must be in the same folder as this script."; exit 1; fi
SHARE_USER="${SUDO_USER:-pi}"

echo "== Installing packages (Samba, Python) =="
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq samba python3 >/dev/null

echo "== Creating folders =="
id -u $SVC_USER >/dev/null 2>&1 || useradd --system --home $ROOT --shell /usr/sbin/nologin $SVC_USER
getent group atelier >/dev/null || groupadd atelier
usermod -aG atelier $SVC_USER
usermod -aG atelier "$SHARE_USER"
mkdir -p $VAULT $DATA/backups /etc/atelier /opt/atelier
chown -R "$SHARE_USER":atelier $VAULT
chmod -R u+rwX,g+rwX,o-rwx $VAULT
find $VAULT -type d -exec chmod g+s {} +
chown -R $SVC_USER:atelier $DATA
chmod 750 $DATA
install -m 644 "$HERE/atelier_server.py" /opt/atelier/atelier_server.py

echo "== Server settings =="
if [[ -f $CONF ]]; then
  TOKEN=$(python3 -c "import json;print(json.load(open('$CONF'))['token'])")
  echo "Keeping the existing token."
else
  TOKEN=$(python3 /opt/atelier/atelier_server.py --new-token)
  cat > $CONF <<EOF
{"db": "$DATA/atelier_index.db", "token": "$TOKEN", "backups": "$DATA/backups", "keep": 14, "host": "0.0.0.0", "port": $PORT}
EOF
fi
chown root:$SVC_USER $CONF
chmod 640 $CONF

# Move an existing vault database (copied over with the vault) to where the server keeps it - also when the
# server has already made an empty one because the vault was copied after a first run.
data_is_empty() {
  [[ ! -f $DATA/atelier_index.db ]] && return 0
  python3 - "$DATA/atelier_index.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
t = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='files'").fetchone()
n = c.execute("SELECT COUNT(*) FROM files").fetchone()[0] if t else 0
sys.exit(0 if n == 0 else 1)
PY
}
for OLD in atelier_index.db atellier_index.db; do
  if [[ -f $VAULT/$OLD ]] && data_is_empty; then
    echo "== Moving the vault database ($OLD) to the server =="
    systemctl stop atelier-server 2>/dev/null || true
    rm -f $DATA/atelier_index.db $DATA/atelier_index.db-wal $DATA/atelier_index.db-shm
    cp -p "$VAULT/$OLD" $DATA/atelier_index.db
    mv "$VAULT/$OLD" "$VAULT/$OLD.moved-to-server"
    chown $SVC_USER:atelier $DATA/atelier_index.db
    break
  fi
done

echo "== Service =="
cat > /etc/systemd/system/atelier-server.service <<EOF
[Unit]
Description=Atelier PDM server
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=atelier
ExecStart=/usr/bin/python3 /opt/atelier/atelier_server.py --config $CONF
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now atelier-server >/dev/null
systemctl restart atelier-server

echo "== Network share =="
if ! grep -q "^\[atelier\]" /etc/samba/smb.conf; then
  cat >> /etc/samba/smb.conf <<EOF

[atelier]
   path = $VAULT
   browseable = yes
   read only = no
   valid users = @atelier
   force group = atelier
   create mask = 0660
   directory mask = 2770
   # Atelier keeps its database on the server, so normal file locking is fine here.
EOF
fi
systemctl restart smbd
if ! pdbedit -L 2>/dev/null | grep -q "^$SHARE_USER:"; then
  echo
  echo "Choose the password people will use to connect to the share (user: $SHARE_USER):"
  smbpasswd -a "$SHARE_USER"
fi

if command -v ufw >/dev/null 2>&1; then
  ufw allow from 192.168.0.0/16 to any port $PORT proto tcp >/dev/null || true
  ufw allow from 192.168.0.0/16 to any app Samba >/dev/null 2>&1 || ufw allow from 192.168.0.0/16 to any port 445 >/dev/null || true
fi

sleep 2
NAME=$(hostname)
IP=$(hostname -I | awk '{print $1}')
echo
if curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || python3 - <<EOF
import urllib.request,sys
sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:$PORT/health",timeout=3).status==200 else 1)
EOF
then STATUS="running"; else STATUS="NOT RUNNING - see: journalctl -u atelier-server -n 50"; fi
cat <<EOF
=============================================================================
 Atelier server: $STATUS
 Vault share:    \\\\$NAME\\atelier      (or \\\\$IP\\atelier)   user: $SHARE_USER
 Server address: http://$NAME.local:$PORT   (or http://$IP:$PORT)
 Server token:   $TOKEN

 Put this in  atelier_config.json  in the vault (the share), then restart Atelier on each PC:
   { "db_server": "http://$IP:$PORT", "db_token": "$TOKEN" }
 Database backups: $DATA/backups (nightly, 14 kept)
=============================================================================
EOF
