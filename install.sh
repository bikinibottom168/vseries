#!/usr/bin/env bash
# Installer for vseries-sync. Run as root on Ubuntu/Debian:
#   sudo bash install.sh
#
# Idempotent: re-running upgrades sync.py / units but preserves existing config.env
# and state.json.

set -euo pipefail

INSTALL_DIR=/opt/vseries-sync
STATE_DIR=/var/lib/vseries-sync
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# -- preflight --
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "ต้องรันด้วย sudo / root" >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "ไม่พบ apt-get — ตัวติดตั้งนี้รองรับเฉพาะ Ubuntu/Debian" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "ไม่พบ systemctl — ต้องใช้ระบบที่เป็น systemd" >&2
    exit 1
fi

# Verify required source files are next to this script
for f in sync.py config.env vseries-sync.service vseries-sync.timer; do
    if [[ ! -f "$SRC_DIR/$f" ]]; then
        echo "ไม่พบไฟล์ที่ต้องใช้: $SRC_DIR/$f" >&2
        exit 1
    fi
done

# -- 1) packages --
echo "[1/7] อัพเดท apt + ติดตั้ง packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    ca-certificates \
    curl \
    jq \
    locales

# -- 2) Python version check (need 3.8+) --
echo "[2/7] ตรวจ Python version"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 8) else 0)')
if [[ "$PY_OK" != "1" ]]; then
    echo "ต้องการ Python 3.8 ขึ้นไป (เจอ $PY_VER)" >&2
    exit 1
fi
echo "    -> python3 $PY_VER OK"

# -- 3) directories --
echo "[3/7] สร้างโฟลเดอร์ $INSTALL_DIR และ $STATE_DIR"
mkdir -p "$INSTALL_DIR" "$STATE_DIR"
chmod 0750 "$STATE_DIR"

# -- 4) venv + requests --
echo "[4/7] สร้าง Python venv และติดตั้ง requests"
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    rm -rf "$INSTALL_DIR/venv"
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet "requests>=2.31,<3"

# -- 5) script --
echo "[5/7] คัดลอก sync.py"
install -m 0755 "$SRC_DIR/sync.py" "$INSTALL_DIR/sync.py"

# Quick syntax check on the installed file (catches typos before systemd runs it)
"$INSTALL_DIR/venv/bin/python" -m py_compile "$INSTALL_DIR/sync.py"
echo "    -> syntax OK"

# -- 6) config --
echo "[6/7] คัดลอก config.env (ถ้ายังไม่มี)"
if [[ -f "$INSTALL_DIR/config.env" ]]; then
    echo "    -> มี config.env อยู่แล้ว ไม่เขียนทับ"
else
    install -m 0600 "$SRC_DIR/config.env" "$INSTALL_DIR/config.env"
    echo "    -> เขียน config.env (chmod 600)"
fi

# -- 7) systemd --
echo "[7/7] ติดตั้ง systemd service + timer"
install -m 0644 "$SRC_DIR/vseries-sync.service" /etc/systemd/system/vseries-sync.service
install -m 0644 "$SRC_DIR/vseries-sync.timer"   /etc/systemd/system/vseries-sync.timer
systemctl daemon-reload
systemctl enable --now vseries-sync.timer

echo
echo "==== ติดตั้งเสร็จ ===="
echo "ตรวจการเชื่อมต่อ (ก่อนรันจริง):"
echo "  sudo bash -c 'set -a; source $INSTALL_DIR/config.env; set +a; \\"
echo "      $INSTALL_DIR/venv/bin/python $INSTALL_DIR/sync.py --check'"
echo
echo "โหมดทดสอบ (3 เรื่อง, ไม่บันทึก state):"
echo "  sudo bash -c 'set -a; source $INSTALL_DIR/config.env; set +a; \\"
echo "      $INSTALL_DIR/venv/bin/python $INSTALL_DIR/sync.py --test'"
echo
echo "รันรอบจริง (manual trigger):"
echo "  sudo systemctl start vseries-sync.service"
echo
echo "ดูสถานะ timer:    systemctl status vseries-sync.timer --no-pager"
echo "ดู log:           journalctl -u vseries-sync.service -n 100 --no-pager"
echo "ดู log สด:        journalctl -u vseries-sync.service -f"
echo "ดู state:         jq . $STATE_DIR/state.json"
echo
echo "อ่านคู่มือเต็ม: manual.txt"
