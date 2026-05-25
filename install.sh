#!/usr/bin/env bash
# Installer for vseries-sync. Run as root on Ubuntu:
#   sudo bash install.sh
#
# Idempotent: re-running upgrades sync.py / units but preserves existing config.env
# and state.json.

set -euo pipefail

INSTALL_DIR=/opt/vseries-sync
STATE_DIR=/var/lib/vseries-sync
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ต้องรันด้วย sudo / root" >&2
    exit 1
fi

echo "[1/6] อัพเดท apt + ติดตั้ง packages ที่จำเป็น"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ca-certificates curl

echo "[2/6] สร้างโฟลเดอร์ $INSTALL_DIR และ $STATE_DIR"
mkdir -p "$INSTALL_DIR" "$STATE_DIR"

echo "[3/6] สร้าง Python venv และติดตั้ง requests"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet "requests>=2.31,<3"

echo "[4/6] คัดลอกไฟล์ sync.py"
install -m 0755 "$SRC_DIR/sync.py" "$INSTALL_DIR/sync.py"

echo "[5/6] คัดลอก config.env (ถ้ายังไม่มี)"
if [[ -f "$INSTALL_DIR/config.env" ]]; then
    echo "    -> มี config.env อยู่แล้ว ไม่เขียนทับ"
else
    install -m 0600 "$SRC_DIR/config.env" "$INSTALL_DIR/config.env"
    echo "    -> เขียน config.env (chmod 600)"
fi

echo "[6/6] ติดตั้ง systemd service + timer"
install -m 0644 "$SRC_DIR/vseries-sync.service" /etc/systemd/system/vseries-sync.service
install -m 0644 "$SRC_DIR/vseries-sync.timer"   /etc/systemd/system/vseries-sync.timer
systemctl daemon-reload
systemctl enable --now vseries-sync.timer

echo
echo "==== ติดตั้งเสร็จ ===="
echo "ดูสถานะ timer:    systemctl status vseries-sync.timer --no-pager"
echo "รันรอบจริง:       sudo systemctl start vseries-sync.service"
echo "ดู log:           journalctl -u vseries-sync.service -n 100 --no-pager"
echo "ดู log สด:        journalctl -u vseries-sync.service -f"
echo "แก้ config:       sudo nano /opt/vseries-sync/config.env"
echo "ดู state:         cat /var/lib/vseries-sync/state.json"
echo
echo "==== โหมดทดสอบ (3 เรื่อง, ไม่บันทึก state) ===="
echo "  sudo bash -c 'set -a; source /opt/vseries-sync/config.env; set +a; \\"
echo "      /opt/vseries-sync/venv/bin/python /opt/vseries-sync/sync.py --test'"
echo
echo "อ่านคู่มือเต็ม: manual.txt"
