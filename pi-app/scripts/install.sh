#!/usr/bin/env bash
# Erst-Installation der Pi-App auf einem Drehkreuz-Pi.
#
# Workflow:
#   1. Skript liest pi-app/devices.json aus dem Repo
#   2. Zeigt eine Liste der konfigurierten Pis zur Auswahl
#   3. Fragt einmalig den Hub-Pi-Token ab
#   4. Installiert apt+pip-Pakete passend zum Reader-Modus
#   5. Schreibt /etc/hotsport-access/config.toml
#   6. Aktiviert + startet die systemd-Services
#
# Aufruf:
#   sudo bash scripts/install.sh                # interaktive Auswahl
#   sudo bash scripts/install.sh hotsport-pi-01 # direkt mit pi_id
#   sudo bash scripts/install.sh -y hotsport-pi-01 TOKEN  # vollautomatisch
#
# Idempotent: bei erneutem Aufruf werden venv und (vorhandene) config.toml
# nicht überschrieben. Für Routine-Updates ist nicht dieses Skript
# zuständig, sondern der hotsport-updater-Service.
set -euo pipefail

INSTALL_ROOT=/opt/hotsport-access
VENV_DIR=${INSTALL_ROOT}/venv
ETC_DIR=/etc/hotsport-access
STATE_DIR=/var/lib/hotsport-access
LOG_DIR=/var/log/hotsport-access
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEVICES_JSON="${REPO_DIR}/devices.json"

# ---------- Argumente parsen (vor EUID-Check, damit --help auch ohne sudo geht) ----------
ASSUME_YES=0
PI_ID=""
PI_TOKEN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0 ;;
        *)
            if [[ -z "${PI_ID}" ]]; then
                PI_ID="$1"
            elif [[ -z "${PI_TOKEN}" ]]; then
                PI_TOKEN="$1"
            fi
            shift ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Bitte als root ausführen (sudo)." >&2
    exit 1
fi

# ---------- TTY-Detection für interaktive Eingaben ----------
HAS_TTY=0
if [[ -r /dev/tty ]] && (echo > /dev/tty) 2>/dev/null; then
    HAS_TTY=1
fi

if [[ ! -f "${DEVICES_JSON}" ]]; then
    echo "FEHLER: ${DEVICES_JSON} nicht gefunden." >&2
    echo "  Wurde das Repo vollständig geklont?" >&2
    exit 1
fi

# ---------- Pi-Auswahl ----------
echo
echo "================================================="
echo " hotsport-access · Pi-Installation"
echo "================================================="
echo

# Liest devices.json mit Python (in Pi OS standardmäßig vorhanden)
if [[ -z "${PI_ID}" ]]; then
    if [[ "${HAS_TTY}" -eq 0 ]]; then
        echo "FEHLER: Kein TTY und keine pi_id als Argument übergeben." >&2
        exit 2
    fi

    echo "Verfügbare Pis aus devices.json:"
    echo
    mapfile -t PI_LIST < <(
        python3 - "${DEVICES_JSON}" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for p in data.get("pis", []):
    print(f"{p['pi_id']}\t{p.get('name','')}\t{p.get('location','')}\t{p.get('reader_mode','keyboard')}\t{p.get('inout','in')}")
PY
    )

    if [[ ${#PI_LIST[@]} -eq 0 ]]; then
        echo "FEHLER: devices.json enthält keine Pis (Feld \"pis\" leer)." >&2
        exit 1
    fi

    i=1
    for line in "${PI_LIST[@]}"; do
        IFS=$'\t' read -r id name location mode inout <<< "${line}"
        printf "  %2d) %-22s  %-20s  %-12s  %-14s  %s\n" \
            "$i" "${id}" "${name}" "${location}" "${mode}" "${inout}"
        i=$((i+1))
    done
    echo

    read -r -p "Auswahl (Nummer 1-${#PI_LIST[@]}): " sel </dev/tty
    if ! [[ "${sel}" =~ ^[0-9]+$ ]] || [[ "${sel}" -lt 1 ]] || [[ "${sel}" -gt ${#PI_LIST[@]} ]]; then
        echo "FEHLER: Ungültige Auswahl." >&2
        exit 2
    fi
    PI_ID="$(echo "${PI_LIST[$((sel-1))]}" | cut -f1)"
fi

# ---------- Pi-Daten aus devices.json holen ----------
read -r PI_NAME PI_LOC MODE INOUT INTERFACE RELAY BUZZER HUB_URL < <(
    python3 - "${DEVICES_JSON}" "${PI_ID}" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
target = sys.argv[2]
match = next((p for p in data.get("pis", []) if p["pi_id"] == target), None)
if not match:
    print("__NOT_FOUND__")
    sys.exit(0)
hub_url = data.get("hub", {}).get("base_url", "")
def esc(v):  # Felder ohne Tabs/Newlines, Default "-"
    s = str(v) if v not in (None, "") else "-"
    return s.replace("\t", " ").replace("\n", " ")
print("\t".join([
    esc(match.get("name")),
    esc(match.get("location")),
    esc(match.get("reader_mode", "keyboard")),
    esc(match.get("inout", "in")),
    esc(match.get("interface_id")),
    esc(match.get("relay_pin", 24)),
    esc(match.get("buzzer_pin", 23)),
    esc(hub_url),
]))
PY
)

if [[ "${PI_NAME}" == "__NOT_FOUND__" ]]; then
    echo "FEHLER: Pi-ID '${PI_ID}' nicht in devices.json gefunden." >&2
    exit 1
fi
if [[ "${HUB_URL}" == "-" || -z "${HUB_URL}" ]]; then
    echo "FEHLER: hub.base_url in devices.json nicht gesetzt." >&2
    exit 1
fi

# ---------- Token abfragen (falls nicht als Argument gesetzt) ----------
if [[ -z "${PI_TOKEN}" ]]; then
    if [[ "${HAS_TTY}" -eq 0 ]]; then
        echo "FEHLER: Kein TTY und kein Token-Argument übergeben." >&2
        exit 2
    fi
    echo
    echo "Hub-Pi-Token (HOTSPORT_HUB_PI_TOKEN aus /etc/hotsport-hub.env auf dem Hub):"
    read -r -s -p "  > " PI_TOKEN </dev/tty 2>/dev/null || PI_TOKEN=""
    echo
fi
if [[ -z "${PI_TOKEN}" ]]; then
    echo "FEHLER: Kein Token angegeben." >&2
    exit 2
fi

# ---------- Übersicht + Bestätigung ----------
echo
echo "Konfiguration:"
echo "  Pi-ID:        ${PI_ID}"
echo "  Name:         ${PI_NAME}"
echo "  Standort:     ${PI_LOC}"
echo "  Reader:       ${MODE}"
echo "  Richtung:     ${INOUT}"
echo "  Interface-ID: ${INTERFACE}"
echo "  Relais-Pin:   ${RELAY}"
echo "  Buzzer-Pin:   ${BUZZER}"
echo "  Hub-URL:      ${HUB_URL}"
echo "  Pi-Token:     **********"
echo

if [[ "${ASSUME_YES}" -eq 0 ]] && [[ "${HAS_TTY}" -eq 1 ]]; then
    read -r -p "Installation starten? [j/N] " confirm </dev/tty 2>/dev/null || confirm=""
    case "${confirm}" in
        j|J|y|Y|ja|JA|yes|YES) ;;
        *) echo "Abgebrochen."; exit 0 ;;
    esac
fi

# ---------- Pakete installieren ----------
echo
echo "==> Pakete installieren"
apt-get update
apt-get install -y python3-venv python3-pip rsync python3-rpi.gpio
case "${MODE}" in
    qr_camera)    apt-get install -y python3-opencv ;;
    rfid_mfrc522) apt-get install -y python3-spidev ;;
esac

# ---------- Verzeichnisse + Bootstrap-Release ----------
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

# ---------- venv ----------
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

# ---------- config.toml schreiben ----------
echo "==> Konfiguration schreiben (${ETC_DIR}/config.toml)"
CONFIG_FILE="${ETC_DIR}/config.toml"
if [[ -f "${CONFIG_FILE}" ]]; then
    cp -a "${CONFIG_FILE}" "${CONFIG_FILE}.bak.$(date +%s)"
    echo "    Bestehende config.toml als ${CONFIG_FILE}.bak.* gesichert."
fi

# Jinja-light: einfaches heredoc mit eingesetzten Variablen
cat > "${CONFIG_FILE}" <<EOF
# /etc/hotsport-access/config.toml
# Automatisch generiert von install.sh aus devices.json.
# Manuelle Änderungen bleiben beim nächsten install.sh-Lauf erhalten,
# wenn diese Datei nicht gelöscht wird (sie wird vorher gesichert).

pi_id     = "${PI_ID}"
name      = "${PI_NAME}"
location  = "${PI_LOC}"

state_dir        = "${STATE_DIR}"
health_bind_host = "127.0.0.1"
health_bind_port = 8765

[hub]
base_url                      = "${HUB_URL}"
pi_token                      = "${PI_TOKEN}"
heartbeat_interval_seconds    = 5.0
update_check_interval_seconds = 30.0
EOF
chmod 0640 "${CONFIG_FILE}"

# ---------- systemd ----------
echo "==> systemd-Units installieren"
install -m 0644 "${REPO_DIR}/systemd/hotsport-access.service" \
    /etc/systemd/system/hotsport-access.service
install -m 0644 "${REPO_DIR}/systemd/hotsport-updater.service" \
    /etc/systemd/system/hotsport-updater.service
systemctl daemon-reload
systemctl enable hotsport-access.service hotsport-updater.service

echo "==> Hardware-Watchdog (BCM2835) – Hinweis"
if ! grep -q "^dtparam=watchdog=on" /boot/config.txt 2>/dev/null \
   && ! grep -q "^dtparam=watchdog=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "    Tipp: dtparam=watchdog=on in /boot/firmware/config.txt eintragen"
    echo "    und in /etc/systemd/system.conf RuntimeWatchdogSec=15 setzen."
fi

echo "==> Service starten"
systemctl restart hotsport-access.service
systemctl restart hotsport-updater.service

echo
echo "================================================="
echo " Fertig. Pi '${PI_ID}' ist eingerichtet."
echo "================================================="
echo "    Logs:    journalctl -u hotsport-access -f"
echo "    Updater: journalctl -u hotsport-updater -f"
echo "    Health:  curl -s http://127.0.0.1:8765/health | jq"
echo
echo "Der Pi sollte innerhalb von ~5 Sekunden im Hub-Dashboard"
echo "  ${HUB_URL}"
echo "auftauchen."
