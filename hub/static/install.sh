#!/usr/bin/env bash
# hotsport-access – Pi Bootstrap-Installer.
#
# Holt den Pi-App-Code als signiertes Release-ZIP vom lokalen Hub und richtet
# alle systemd-Units, venv und Konfiguration ein. Kein Internet, kein Git nötig.
#
# Einfachster Aufruf (Token wird interaktiv abgefragt):
#   curl -fsSL http://HUB:PORT/install.sh | sudo bash
#
# Mit Token als Argument (nicht-interaktiv):
#   curl -fsSL http://HUB:PORT/install.sh | sudo bash -s -- TOKEN
#
# Idempotent: bei erneutem Aufruf werden venv und Konfig NICHT überschrieben.
# Für Routine-Updates ist der `hotsport-updater`-Service zuständig.

set -euo pipefail

# ---------------- Defaults ----------------
# DEFAULT_HUB_URL wird vom Hub beim Ausliefern automatisch ersetzt
# (Marker: __HOTSPORT_HUB_URL__). Ohne den Hub-Endpunkt ist der Default leer.
DEFAULT_HUB_URL="__HOTSPORT_HUB_URL__"

HUB_URL=""
HUB_TOKEN=""
PI_ID=""
PI_NAME=""
PI_LOCATION=""
MODE=""
ASSUME_YES=0

# Erstes positionales Argument (ohne --) ist das Token – das ist der Normalfall.
# Lange Flags (--token, --hub …) bleiben für Skripting erhalten.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --hub)        HUB_URL="$2"; shift 2 ;;
        --token)      HUB_TOKEN="$2"; shift 2 ;;
        --pi-id)      PI_ID="$2"; shift 2 ;;
        --name)       PI_NAME="$2"; shift 2 ;;
        --location)   PI_LOCATION="$2"; shift 2 ;;
        --mode)       MODE="$2"; shift 2 ;;
        -y|--yes)     ASSUME_YES=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \?//'
            exit 0 ;;
        --) shift; break ;;
        --*) echo "Unbekanntes Argument: $1" >&2; exit 2 ;;
        *)
            # Erstes Positional = Token, weitere ignorieren (Tippfehlerschutz)
            if [[ -z "${HUB_TOKEN}" ]]; then
                HUB_TOKEN="$1"
            fi
            shift ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Bitte als root ausführen (sudo bash …)." >&2
    exit 1
fi

# ---------------- Defaults füllen / interaktiv abfragen ----------------

# Hub-URL: vorbelegt vom Hub-Endpunkt. Wenn der Marker noch da steht
# (lokales Ausführen einer ungerenderten Kopie), interaktiv abfragen.
# Wir prüfen auf gültiges URL-Schema, statt Stringvergleich gegen den
# Marker – sonst stolpern wir über String-Replace im Endpunkt selbst.
if [[ -z "${HUB_URL}" ]] && [[ "${DEFAULT_HUB_URL}" =~ ^https?:// ]]; then
    HUB_URL="${DEFAULT_HUB_URL}"
fi

# TTY-Test: gibt es ein echtes Terminal für interaktive Eingaben?
# Bei `curl | bash` ist stdin die Pipe, daher müssen wir /dev/tty verwenden –
# das funktioniert aber nur, wenn ein Controlling-Terminal hängt (SSH, Konsole).
HAS_TTY=0
if [[ -r /dev/tty ]] && (echo > /dev/tty) 2>/dev/null; then
    HAS_TTY=1
fi

ask() {
    # ask <var> <prompt> <default>
    local __var="$1" __prompt="$2" __default="$3" __value=""
    if [[ "${HAS_TTY}" -eq 1 ]]; then
        local __prompt_full
        if [[ -n "${__default}" ]]; then
            __prompt_full="${__prompt} [${__default}]: "
        else
            __prompt_full="${__prompt}: "
        fi
        # Lesen kann fehlschlagen (Device not configured) – Default greift dann.
        read -r -p "${__prompt_full}" __value </dev/tty 2>/dev/null || __value=""
    fi
    printf -v "${__var}" "%s" "${__value:-${__default}}"
}

PI_ID=${PI_ID:-$(hostname)}
[[ -z "${PI_NAME}"     ]] && PI_NAME="${PI_ID}"
[[ -z "${PI_LOCATION}" ]] && PI_LOCATION="hotsport"
[[ -z "${MODE}"        ]] && MODE="keyboard"

if [[ -z "${HUB_URL}" ]]; then
    ask HUB_URL "Hub-URL (z.B. http://hub.local:8000)" ""
fi
if [[ -z "${HUB_TOKEN}" ]] && [[ "${HAS_TTY}" -eq 1 ]]; then
    echo "Pi-Token (aus /etc/hotsport-hub.env auf dem Hub):"
    read -r -s -p "  > " HUB_TOKEN </dev/tty 2>/dev/null || HUB_TOKEN=""
    echo
fi

if [[ -z "${HUB_URL}" || -z "${HUB_TOKEN}" ]]; then
    echo "FEHLER: Hub-URL und Token sind erforderlich." >&2
    echo "  Aufruf: curl -fsSL http://HUB:PORT/install.sh | sudo bash -s -- TOKEN" >&2
    exit 2
fi

HUB_URL=${HUB_URL%/}

case "${MODE}" in
    keyboard|qr_camera|rfid_mfrc522) ;;
    *) echo "FEHLER: --mode muss keyboard, qr_camera oder rfid_mfrc522 sein." >&2; exit 2 ;;
esac

# ---------------- Pfade ----------------

INSTALL_ROOT=/opt/hotsport-access
RELEASES_DIR=${INSTALL_ROOT}/releases
VENV_DIR=${INSTALL_ROOT}/venv
ETC_DIR=/etc/hotsport-access
STATE_DIR=/var/lib/hotsport-access
LOG_DIR=/var/log/hotsport-access
SERVICE_USER=hotsport

echo "============================================================"
echo " hotsport-access · Pi Bootstrap-Installer"
echo "------------------------------------------------------------"
echo "  Hub-URL:    ${HUB_URL}"
echo "  Pi-ID:      ${PI_ID}"
echo "  Name:       ${PI_NAME}"
echo "  Standort:   ${PI_LOCATION}"
echo "  Modus:      ${MODE}"
echo "============================================================"

# Bestätigungsabfrage – nur wenn TTY verfügbar und nicht --yes gesetzt
if [[ "${ASSUME_YES}" -eq 0 ]] && [[ "${HAS_TTY}" -eq 1 ]]; then
    confirm=""
    read -r -p "Installation starten? [j/N] " confirm </dev/tty 2>/dev/null || confirm=""
    case "${confirm}" in
        j|J|y|Y|ja|JA|yes|YES) ;;
        *) echo "Abgebrochen."; exit 0 ;;
    esac
fi

# ---------------- 1. APT-Pakete ----------------

echo "==> APT-Pakete installieren"
export DEBIAN_FRONTEND=noninteractive
apt-get update
APT_PKGS=(curl unzip ca-certificates python3-venv python3-pip rsync python3-rpi.gpio)
case "${MODE}" in
    qr_camera)    APT_PKGS+=(python3-opencv) ;;
    rfid_mfrc522) APT_PKGS+=(python3-spidev) ;;
esac
apt-get install -y "${APT_PKGS[@]}"

# ---------------- 2. Service-User & Verzeichnisse ----------------

echo "==> Service-User '${SERVICE_USER}' anlegen (falls fehlend)"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
# Zugriff auf GPIO/SPI/Input für Reader und Relais
for grp in gpio spi input dialout video; do
    if getent group "${grp}" >/dev/null 2>&1; then
        usermod -aG "${grp}" "${SERVICE_USER}" || true
    fi
done

echo "==> Verzeichnisse anlegen"
install -d -m 0755 "${INSTALL_ROOT}" "${RELEASES_DIR}"
install -d -m 0750 "${ETC_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}" "${LOG_DIR}"

# ---------------- 3. Bootstrap-Config schreiben ----------------

CONFIG_FILE=${ETC_DIR}/config.toml
if [[ -f "${CONFIG_FILE}" ]]; then
    echo "==> ${CONFIG_FILE} existiert – nicht überschrieben"
else
    echo "==> ${CONFIG_FILE} schreiben"
    umask 027
    cat > "${CONFIG_FILE}" <<TOML
# /etc/hotsport-access/config.toml
# Bootstrap-Identität dieses Pis. Alle weiteren Einstellungen kommen vom Hub.
pi_id    = "${PI_ID}"
name     = "${PI_NAME}"
location = "${PI_LOCATION}"

state_dir         = "${STATE_DIR}"
health_bind_host  = "127.0.0.1"
health_bind_port  = 8765

[hub]
base_url                       = "${HUB_URL}"
pi_token                       = "${HUB_TOKEN}"
heartbeat_interval_seconds     = 5.0
update_check_interval_seconds  = 30.0
TOML
    chown root:"${SERVICE_USER}" "${CONFIG_FILE}"
    chmod 0640 "${CONFIG_FILE}"
fi

# ---------------- 4. Aktuelles Release vom Hub holen ----------------

echo "==> Release vom Hub holen (${HUB_URL}/api/latest-release)"
TMP=$(mktemp -d)
trap 'rm -rf "${TMP}"' EXIT

# Metadaten holen
curl -fsSL -H "Authorization: Bearer ${HUB_TOKEN}" \
    "${HUB_URL}/api/latest-release" -o "${TMP}/meta.json"

VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "${TMP}/meta.json")
ZIP_URL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "${TMP}/meta.json")
EXPECTED_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "${TMP}/meta.json")

if [[ -z "${VERSION}" || "${VERSION}" == "None" ]]; then
    echo "FEHLER: Hub hat keine Releases. Erst im Dashboard ein Release hochladen." >&2
    exit 3
fi

echo "    Version: ${VERSION}"
echo "    URL:     ${ZIP_URL}"

curl -fsSL -H "Authorization: Bearer ${HUB_TOKEN}" "${ZIP_URL}" -o "${TMP}/release.zip"

ACTUAL_SHA=$(sha256sum "${TMP}/release.zip" | awk '{print $1}')
if [[ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]]; then
    echo "FEHLER: SHA-256 stimmt nicht überein!" >&2
    echo "  erwartet: ${EXPECTED_SHA}" >&2
    echo "  erhalten: ${ACTUAL_SHA}"   >&2
    exit 4
fi
echo "    SHA-256: OK"

# Entpacken nach /opt/hotsport-access/releases/<version>/
TARGET_DIR=${RELEASES_DIR}/${VERSION}
if [[ -d "${TARGET_DIR}" ]]; then
    echo "==> Release ${VERSION} schon entpackt – überspringe"
else
    echo "==> Entpacken nach ${TARGET_DIR}"
    install -d -m 0755 "${TARGET_DIR}"
    unzip -q "${TMP}/release.zip" -d "${TMP}/extract"
    INNER=$(find "${TMP}/extract" -mindepth 1 -maxdepth 1 -type d | head -n1)
    rsync -a "${INNER}/" "${TARGET_DIR}/"
fi

ln -sfn "${TARGET_DIR}" "${INSTALL_ROOT}/current"

# ---------------- 5. venv & Python-Pakete ----------------

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "==> venv anlegen"
    # --system-site-packages: damit RPi.GPIO/python3-opencv aus apt nutzbar sind
    python3 -m venv --system-site-packages "${VENV_DIR}"
fi

echo "==> Python-Abhängigkeiten installieren"
"${VENV_DIR}/bin/pip" install --upgrade --quiet pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_ROOT}/current/requirements.txt"
case "${MODE}" in
    qr_camera)
        "${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_ROOT}/current/requirements-camera.txt"
        ;;
    rfid_mfrc522)
        "${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_ROOT}/current/requirements-rfid.txt"
        ;;
esac

chown -R "${SERVICE_USER}":"${SERVICE_USER}" "${INSTALL_ROOT}"

# ---------------- 6. systemd-Units ----------------

echo "==> systemd-Units schreiben"
cat > /etc/systemd/system/hotsport-access.service <<UNIT
[Unit]
Description=Hotsport Access – Drehkreuz-Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=60s
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_ROOT}/current
Environment=PYTHONPATH=${INSTALL_ROOT}/current
Environment=HOTSPORT_ACCESS_CONFIG=${CONFIG_FILE}
ExecStart=${VENV_DIR}/bin/python -m app.main
Restart=on-failure
RestartSec=2s
StateDirectory=hotsport-access
LogsDirectory=hotsport-access

# Sandbox
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=${STATE_DIR} ${LOG_DIR}

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/hotsport-updater.service <<UNIT
[Unit]
Description=Hotsport Access – Updater
After=network-online.target hotsport-access.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_ROOT}
Environment=PYTHONPATH=${INSTALL_ROOT}/current
Environment=HOTSPORT_ACCESS_CONFIG=${CONFIG_FILE}
Environment=HOTSPORT_INSTALL_ROOT=${INSTALL_ROOT}
ExecStart=${VENV_DIR}/bin/python -m updater.updater
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload

# ---------------- 7. Optional: Hardware-Watchdog Hinweis ----------------

CONFIG_TXT=""
for f in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$f" ]] && CONFIG_TXT="$f" && break
done
if [[ -n "${CONFIG_TXT}" ]]; then
    if ! grep -q '^dtparam=watchdog=on' "${CONFIG_TXT}"; then
        echo "    Tipp: für extra Robustheit 'dtparam=watchdog=on' in ${CONFIG_TXT}"
        echo "    eintragen und 'RuntimeWatchdogSec=15' in /etc/systemd/system.conf setzen."
    fi
fi

# ---------------- 8. Services starten ----------------

echo "==> Services aktivieren und starten"
systemctl enable --now hotsport-access.service
systemctl enable --now hotsport-updater.service

# ---------------- 9. Verifikation ----------------

sleep 3
echo
echo "============================================================"
echo " Installation abgeschlossen."
echo "------------------------------------------------------------"
echo "  Pi-ID:      ${PI_ID}"
echo "  Version:    ${VERSION}"
echo
echo "  Logs anzeigen:"
echo "    journalctl -u hotsport-access -f"
echo
echo "  Erster Heartbeat-Check:"
if curl -fsS --max-time 3 "http://127.0.0.1:8765/health" >/dev/null 2>&1; then
    echo "    [✓] lokaler Health-Endpunkt antwortet"
else
    echo "    […] noch nicht erreichbar – wenige Sekunden warten und erneut prüfen"
fi
echo
echo "  Im Dashboard sollte ${PI_ID} jetzt erscheinen:"
echo "    ${HUB_URL}/"
echo
echo "  Nächster Schritt: im Dashboard interface_id, Reader-Modus etc. setzen."
echo "============================================================"
