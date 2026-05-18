#!/usr/bin/env bash
# Erst-Installation der Pi-App auf einem Drehkreuz-Pi.
#
# Idempotent: bei erneutem Aufruf werden venv und Konfiguration NICHT überschrieben,
# nur Code-Dateien aus dem Repo nach /opt/hotsport-access/current/ kopiert.
# Für Routine-Updates ist *nicht* dieses Skript zuständig, sondern der Updater.
set -euo pipefail

INSTALL_ROOT=/opt/hotsport-access
VENV_DIR=${INSTALL_ROOT}/venv
ETC_DIR=/etc/hotsport-access
STATE_DIR=/var/lib/hotsport-access
LOG_DIR=/var/log/hotsport-access
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Bitte als root ausführen (sudo)." >&2
    exit 1
fi

MODE=${1:-keyboard}   # keyboard | qr_camera | rfid_mfrc522

echo "==> Pakete installieren"
apt-get update
apt-get install -y python3-venv python3-pip rsync python3-rpi.gpio
case "${MODE}" in
    qr_camera)    apt-get install -y python3-opencv ;;
    rfid_mfrc522) apt-get install -y python3-spidev ;;
esac

echo "==> Verzeichnisse anlegen"
BOOTSTRAP_DIR="${INSTALL_ROOT}/releases/bootstrap"
install -d -m 0755 "${INSTALL_ROOT}" "${INSTALL_ROOT}/releases" "${BOOTSTRAP_DIR}"
install -d -m 0755 "${STATE_DIR}" "${LOG_DIR}"
install -d -m 0750 "${ETC_DIR}"

echo "==> Erst-Release nach ${BOOTSTRAP_DIR} kopieren"
rsync -a --delete \
    --exclude __pycache__ --exclude '*.pyc' \
    "${REPO_DIR}/app/"     "${BOOTSTRAP_DIR}/app/"
rsync -a --delete --exclude __pycache__ \
    "${REPO_DIR}/updater/" "${BOOTSTRAP_DIR}/updater/"
install -m 0644 "${REPO_DIR}/requirements.txt" "${BOOTSTRAP_DIR}/requirements.txt"
echo "bootstrap" > "${BOOTSTRAP_DIR}/VERSION"

ln -sfn "${BOOTSTRAP_DIR}" "${INSTALL_ROOT}/current"

echo "==> venv aufsetzen"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    python3 -m venv --system-site-packages "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${BOOTSTRAP_DIR}/requirements.txt"
case "${MODE}" in
    qr_camera)
        "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements-camera.txt"
        ;;
    rfid_mfrc522)
        "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements-rfid.txt"
        ;;
esac

echo "==> Konfiguration"
if [[ ! -f "${ETC_DIR}/config.toml" ]]; then
    install -m 0640 "${REPO_DIR}/config/config.example.toml" "${ETC_DIR}/config.toml"
    echo "    -> ${ETC_DIR}/config.toml angelegt – bitte pi_id/Token/interface_id anpassen."
fi

echo "==> systemd-Units installieren"
install -m 0644 "${REPO_DIR}/systemd/hotsport-access.service" \
    /etc/systemd/system/hotsport-access.service
install -m 0644 "${REPO_DIR}/systemd/hotsport-updater.service" \
    /etc/systemd/system/hotsport-updater.service
systemctl daemon-reload
systemctl enable hotsport-access.service hotsport-updater.service

echo "==> Hardware-Watchdog (BCM2835) aktivieren (optional)"
if ! grep -q "^dtparam=watchdog=on" /boot/config.txt 2>/dev/null \
   && ! grep -q "^dtparam=watchdog=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "    Tipp: dtparam=watchdog=on in /boot/firmware/config.txt eintragen"
    echo "    und in /etc/systemd/system.conf RuntimeWatchdogSec=15 setzen."
fi

echo "==> Service starten"
systemctl restart hotsport-access.service
systemctl restart hotsport-updater.service

echo "==> Fertig."
echo "    Logs:    journalctl -u hotsport-access -f"
echo "    Updater: journalctl -u hotsport-updater -f"
echo "    Health:  curl -s http://127.0.0.1:8765/health | jq"
