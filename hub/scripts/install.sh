#!/usr/bin/env bash
# Erst-Installation des Hubs auf einem Raspberry Pi / Mini-PC im LAN.
# Idempotent – kann zur Reparatur erneut ausgeführt werden.
set -euo pipefail

INSTALL_DIR=/opt/hotsport-hub
DATA_DIR=/var/lib/hotsport-hub
LOG_DIR=/var/log/hotsport-hub
ETC_DIR=/etc/hotsport-hub
USER_NAME=hotsport

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Bitte als root ausführen (sudo)." >&2
    exit 1
fi

echo "==> Pakete installieren"
apt-get update
apt-get install -y python3-venv python3-pip

echo "==> Benutzer ${USER_NAME} anlegen (falls fehlt)"
if ! id -u "${USER_NAME}" >/dev/null 2>&1; then
    useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${USER_NAME}"
fi

echo "==> Verzeichnisse anlegen"
install -d -o root        -g root        -m 0755 "${INSTALL_DIR}"
install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0755 "${DATA_DIR}" "${DATA_DIR}/releases"
install -d -o "${USER_NAME}" -g "${USER_NAME}" -m 0755 "${LOG_DIR}"
install -d -o root        -g "${USER_NAME}" -m 0750 "${ETC_DIR}"

echo "==> App-Dateien synchronisieren"
rsync -a --delete \
    --exclude __pycache__ \
    "${REPO_DIR}/app/"        "${INSTALL_DIR}/app/"
rsync -a --delete "${REPO_DIR}/templates/" "${INSTALL_DIR}/templates/"
rsync -a --delete "${REPO_DIR}/static/"    "${INSTALL_DIR}/static/"
install -m 0644 "${REPO_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
chown -R root:root "${INSTALL_DIR}/app" "${INSTALL_DIR}/templates" "${INSTALL_DIR}/static"

echo "==> venv aufsetzen"
if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "==> Konfiguration"
if [[ ! -f "${ETC_DIR}/hub.env" ]]; then
    install -m 0640 -o root -g "${USER_NAME}" \
        "${REPO_DIR}/systemd/hub.env.example" "${ETC_DIR}/hub.env"
    echo "    -> ${ETC_DIR}/hub.env wurde aus dem Beispiel angelegt."
    echo "       BITTE Token & Dashboard-Passwort anpassen."
fi

echo "==> systemd-Unit installieren"
install -m 0644 "${REPO_DIR}/systemd/hotsport-hub.service" \
    /etc/systemd/system/hotsport-hub.service
systemctl daemon-reload
systemctl enable hotsport-hub.service
systemctl restart hotsport-hub.service

echo "==> Fertig."
echo "    Dashboard:   http://$(hostname -I | awk '{print $1}'):${HOTSPORT_HUB_PORT:-8000}/"
echo "    Logs:        journalctl -u hotsport-hub -f"
echo "    Config:      ${ETC_DIR}/hub.env"
