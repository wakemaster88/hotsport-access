#!/usr/bin/env bash
# Erst-Installation der Pi-App auf einem Drehkreuz-Pi.
#
# Workflow:
#   1. Skript liest pi-app/devices.json aus dem Repo
#   2. Zeigt eine Liste der konfigurierten Pis zur Auswahl
#   3. Fragt einmalig den Binarytec-API-Bearer-Token ab (Pflicht)
#   4. Fragt optional den Hub-Pi-Token ab (leer = Standalone, kein Hub)
#   5. Installiert apt+pip-Pakete passend zum Reader-Modus
#   6. Schreibt /etc/hotsport-access/config.toml mit kompletter Live-Config
#   7. Aktiviert + startet die systemd-Services
#
# Aufruf:
#   sudo bash scripts/install.sh                     # interaktive Auswahl
#   sudo bash scripts/install.sh hotsport-pi-01      # mit Pi-ID
#   sudo bash scripts/install.sh -y hotsport-pi-01 API_TOKEN [HUB_TOKEN]
#
# Idempotent: zweiter Lauf sichert vorhandene config.toml als .bak.* und
# schreibt eine neue.
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
API_TOKEN=""
HUB_TOKEN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0 ;;
        *)
            if [[ -z "${PI_ID}" ]]; then
                PI_ID="$1"
            elif [[ -z "${API_TOKEN}" ]]; then
                API_TOKEN="$1"
            elif [[ -z "${HUB_TOKEN}" ]]; then
                HUB_TOKEN="$1"
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

echo
echo "================================================="
echo " hotsport-access · Pi-Installation"
echo "================================================="
echo

# ---------- Pi-Auswahl ----------
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

# ---------- Pi-Daten + globale Sections aus devices.json holen ----------
# Output: 17 Tab-getrennte Felder. IFS=$'\t' wichtig, sonst splittet Bash auch
# an Leerzeichen ("Eingang Nord" -> "Eingang" + "Nord").
IFS=$'\t' read -r \
    PI_NAME PI_LOC MODE INOUT INTERFACE \
    RELAY_PIN RELAY_PULSE BUZZER_PIN \
    READER_DEVICE READER_CAMIDX \
    HUB_URL HUB_TOKEN_FROM_JSON HUB_DISCOVER HUB_PORT HUB_DISCOVER_INTERVAL \
    API_BASE_URL API_TOKEN_FROM_JSON API_VERIFY_TLS API_CONNECT_TIMEOUT API_REQUEST_TIMEOUT \
    < <(
    python3 - "${DEVICES_JSON}" "${PI_ID}" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
target = sys.argv[2]
match = next((p for p in data.get("pis", []) if p["pi_id"] == target), None)
if not match:
    print("__NOT_FOUND__")
    sys.exit(0)
defaults = data.get("defaults") or {}
api = data.get("api") or {}
hub = data.get("hub") or {}

def pick(*keys, default=""):
    """Erste nicht-leere Quelle aus match -> defaults zurückgeben."""
    for k in keys:
        for src in (match, defaults):
            v = src.get(k)
            if v not in (None, ""):
                return v
    return default

def esc(v):
    if v is None or v == "":
        s = "-"
    elif isinstance(v, bool):
        # TOML braucht "false"/"true" in Kleinschreibung.
        s = "true" if v else "false"
    else:
        s = str(v)
    return s.replace("\t", " ").replace("\n", " ")

print("\t".join([
    esc(match.get("name")),
    esc(match.get("location")),
    esc(pick("reader_mode", default="keyboard")),
    esc(match.get("inout", "in")),
    esc(match.get("interface_id")),
    esc(pick("relay_pin", default=24)),
    esc(pick("relay_pulse_seconds", default=1.0)),
    esc(pick("buzzer_pin", default=23)),
    esc(pick("reader_device_path", default="/dev/input/event0")),
    esc(pick("reader_camera_index", default=0)),
    esc(hub.get("base_url", "")),
    esc(hub.get("pi_token", "")),
    esc(hub.get("discover", False)),
    esc(hub.get("hub_port", 8000)),
    esc(hub.get("discover_interval_seconds", 15.0)),
    esc(api.get("base_url", "")),
    esc(api.get("bearer_token", "")),
    esc(api.get("verify_tls", False)),
    esc(api.get("connect_timeout_seconds", 1.0)),
    esc(api.get("request_timeout_seconds", 2.0)),
]))
PY
)

if [[ "${PI_NAME}" == "__NOT_FOUND__" ]]; then
    echo "FEHLER: Pi-ID '${PI_ID}' nicht in devices.json gefunden." >&2
    exit 1
fi
if [[ "${API_BASE_URL}" == "-" || -z "${API_BASE_URL}" ]]; then
    echo "FEHLER: api.base_url in devices.json nicht gesetzt." >&2
    exit 1
fi

# Defaults für leere Felder ("-" -> sinnvoller Standardwert)
[[ "${HUB_URL}"              == "-" ]] && HUB_URL=""
[[ "${HUB_TOKEN_FROM_JSON}"  == "-" ]] && HUB_TOKEN_FROM_JSON=""
[[ "${HUB_DISCOVER}"         == "-" ]] && HUB_DISCOVER="false"
[[ "${HUB_PORT}"             == "-" ]] && HUB_PORT="8000"
[[ "${HUB_DISCOVER_INTERVAL}" == "-" ]] && HUB_DISCOVER_INTERVAL="15.0"
if [[ "${HUB_PORT}" == "8765" ]]; then
    echo "WARNUNG: hub_port=8765 ist der Pi-Health-Port – setze hub_port auf 8000 (Hub-Dashboard)." >&2
    HUB_PORT="8000"
fi
[[ "${API_TOKEN_FROM_JSON}"  == "-" ]] && API_TOKEN_FROM_JSON=""
[[ "${INTERFACE}"            == "-" ]] && INTERFACE=""
[[ "${API_VERIFY_TLS}"       == "-" ]] && API_VERIFY_TLS="false"

# ---------- Tokens aus existierender config.toml/secrets-Datei holen ----------
# Damit ist Re-Install vollkommen automatisch: Beim ersten Mal werden die
# Tokens interaktiv abgefragt + nach /etc/hotsport-access/config.toml
# geschrieben; bei jedem weiteren `git pull && sudo bash install.sh` werden
# sie von dort wieder gelesen.
EXISTING_CFG="/etc/hotsport-access/config.toml"
EXISTING_SECRETS="/etc/hotsport-access/tokens.env"
EXISTING_API_TOKEN=""
EXISTING_HUB_TOKEN=""
if [[ -r "${EXISTING_CFG}" ]]; then
    EXISTING_API_TOKEN="$(awk -F'"' '/^bearer_token[[:space:]]*=/{print $2; exit}' "${EXISTING_CFG}" 2>/dev/null || true)"
    EXISTING_HUB_TOKEN="$(awk -F'"' '/^pi_token[[:space:]]*=/{print $2; exit}' "${EXISTING_CFG}" 2>/dev/null || true)"
fi
if [[ -z "${EXISTING_API_TOKEN}" && -r "${EXISTING_SECRETS}" ]]; then
    # shellcheck disable=SC1090
    source "${EXISTING_SECRETS}" 2>/dev/null || true
    EXISTING_API_TOKEN="${API_TOKEN_FROM_FILE:-${HOTSPORT_API_TOKEN:-${EXISTING_API_TOKEN}}}"
    EXISTING_HUB_TOKEN="${HUB_TOKEN_FROM_FILE:-${HOTSPORT_HUB_TOKEN:-${EXISTING_HUB_TOKEN}}}"
fi

# ---------- API-Bearer-Token: Argument > Env > vorhandene config.toml > devices.json > interaktiv ----------
API_TOKEN_SOURCE="argument"
if [[ -z "${API_TOKEN}" ]] && [[ -n "${HOTSPORT_API_TOKEN:-}" ]]; then
    API_TOKEN="${HOTSPORT_API_TOKEN}"
    API_TOKEN_SOURCE="env (HOTSPORT_API_TOKEN)"
fi
if [[ -z "${API_TOKEN}" ]] && [[ -n "${EXISTING_API_TOKEN}" ]]; then
    API_TOKEN="${EXISTING_API_TOKEN}"
    API_TOKEN_SOURCE="vorhandene config.toml"
fi
if [[ -z "${API_TOKEN}" ]] && [[ -n "${API_TOKEN_FROM_JSON}" ]]; then
    API_TOKEN="${API_TOKEN_FROM_JSON}"
    API_TOKEN_SOURCE="devices.json"
fi
if [[ -z "${API_TOKEN}" ]]; then
    if [[ "${HAS_TTY}" -eq 0 ]]; then
        echo "FEHLER: Kein TTY und kein API-Bearer-Token übergeben." >&2
        echo "Wege ihn zu setzen:" >&2
        echo "  1) Argument:    sudo bash install.sh -y ${PI_ID} <API_TOKEN> [HUB_TOKEN]" >&2
        echo "  2) Env-Var:     HOTSPORT_API_TOKEN=... HOTSPORT_HUB_TOKEN=... sudo -E bash install.sh -y ${PI_ID}" >&2
        echo "  3) devices.json: api.bearer_token (nur in privatem Repo!)" >&2
        exit 2
    fi
    echo
    echo "Binarytec-API-Bearer-Token (Pflicht – ohne den können keine Scans validiert werden):"
    echo "  Hinweis: nach dem Erst-Setup wird der Token automatisch wieder"
    echo "           aus /etc/hotsport-access/config.toml gelesen, du musst"
    echo "           ihn also nur ein einziges Mal eingeben."
    read -r -s -p "  > " API_TOKEN </dev/tty 2>/dev/null || API_TOKEN=""
    echo
    API_TOKEN_SOURCE="interaktiv"
fi
if [[ -z "${API_TOKEN}" ]]; then
    echo "FEHLER: Kein API-Token angegeben." >&2
    exit 2
fi

# ---------- Hub-Token: Argument > Env > vorhandene config.toml > devices.json > interaktiv ----------
HUB_TOKEN_SOURCE="argument"
if [[ -z "${HUB_TOKEN}" ]] && [[ -n "${HOTSPORT_HUB_TOKEN:-}" ]]; then
    HUB_TOKEN="${HOTSPORT_HUB_TOKEN}"
    HUB_TOKEN_SOURCE="env (HOTSPORT_HUB_TOKEN)"
fi
if [[ -z "${HUB_TOKEN}" ]] && [[ -n "${EXISTING_HUB_TOKEN}" ]]; then
    HUB_TOKEN="${EXISTING_HUB_TOKEN}"
    HUB_TOKEN_SOURCE="vorhandene config.toml"
fi
if [[ -z "${HUB_TOKEN}" ]] && [[ -n "${HUB_TOKEN_FROM_JSON}" ]]; then
    HUB_TOKEN="${HUB_TOKEN_FROM_JSON}"
    HUB_TOKEN_SOURCE="devices.json"
fi
if [[ -z "${HUB_TOKEN}" ]] && [[ -n "${HUB_URL}" ]] && [[ "${HAS_TTY}" -eq 1 ]]; then
    echo
    echo "Hub-Pi-Token (optional, leer lassen = Standalone-Modus ohne Dashboard-Heartbeat):"
    read -r -s -p "  > " HUB_TOKEN </dev/tty 2>/dev/null || HUB_TOKEN=""
    echo
    HUB_TOKEN_SOURCE="interaktiv"
fi

# ---------- Übersicht + Bestätigung ----------
echo
echo "Konfiguration:"
echo "  Pi-ID:        ${PI_ID}"
echo "  Name:         ${PI_NAME}"
echo "  Standort:     ${PI_LOC}"
echo "  Reader:       ${MODE} (device=${READER_DEVICE}, cam=${READER_CAMIDX})"
echo "  Richtung:     ${INOUT}"
echo "  Interface-ID: ${INTERFACE}"
echo "  Relais/Buzzer: GPIO${RELAY_PIN} (puls ${RELAY_PULSE}s) / GPIO${BUZZER_PIN}"
echo "  API:          ${API_BASE_URL} (verify_tls=${API_VERIFY_TLS})"
echo "  API-Token:    ********** (Quelle: ${API_TOKEN_SOURCE})"
if [[ -n "${HUB_TOKEN}" ]]; then
    if [[ "${HUB_DISCOVER}" == "true" ]] || [[ "${HUB_URL}" == "auto" ]] || [[ "${HUB_URL}" == "discover" ]] || [[ -z "${HUB_URL}" ]]; then
        echo "  Hub:          automatische LAN-Suche (Port ${HUB_PORT})"
    else
        echo "  Hub:          ${HUB_URL}"
    fi
    echo "  Hub-Token:    ********** (Quelle: ${HUB_TOKEN_SOURCE})"
else
    echo "  Hub:          (kein Hub-Token – Standalone-Modus)"
fi
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

# Pi OS Lite hat oft 'packagekit' (oder 'unattended-upgrades') aktiv, die den
# dpkg-Lock im Hintergrund halten. Wir stoppen sie kurz und reaktivieren sie
# am Ende. Wenn die Units gar nicht existieren, sind die Aufrufe No-Op.
PAUSED_UNITS=()
for unit in packagekit unattended-upgrades apt-daily.service apt-daily-upgrade.service; do
    if systemctl is-active --quiet "${unit}" 2>/dev/null; then
        echo "    Pause: ${unit}"
        systemctl stop "${unit}" 2>/dev/null || true
        PAUSED_UNITS+=("${unit}")
    fi
done

for i in {1..30}; do
    if ! fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
       && ! fuser /var/lib/apt/lists/lock     >/dev/null 2>&1; then
        break
    fi
    echo "    Warte auf dpkg-Lock... (${i}/30)"
    sleep 2
done

apt-get update
apt-get install -y python3-venv python3-pip rsync python3-rpi.gpio
case "${MODE}" in
    qr_camera)    apt-get install -y python3-opencv ;;
    rfid_mfrc522) apt-get install -y python3-spidev ;;
esac

for unit in "${PAUSED_UNITS[@]}"; do
    echo "    Reaktiviere: ${unit}"
    systemctl start "${unit}" 2>/dev/null || true
done

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

# ---------- config.toml schreiben (komplett, mit allen Live-Sections) ----------
echo "==> Konfiguration schreiben (${ETC_DIR}/config.toml)"
CONFIG_FILE="${ETC_DIR}/config.toml"
if [[ -f "${CONFIG_FILE}" ]]; then
    cp -a "${CONFIG_FILE}" "${CONFIG_FILE}.bak.$(date +%s)"
    echo "    Bestehende config.toml als ${CONFIG_FILE}.bak.* gesichert."
fi

# Cache löschen, damit eine geänderte devices.json beim nächsten Start greift.
# Sonst würde der Pi noch die zuletzt vom Hub gepullte (alte) Config nehmen.
LIVE_CACHE="${STATE_DIR}/live_config.json"
if [[ -f "${LIVE_CACHE}" ]]; then
    cp -a "${LIVE_CACHE}" "${LIVE_CACHE}.bak.$(date +%s)"
    rm -f "${LIVE_CACHE}"
    echo "    Live-Config-Cache geleert (alte Sicherung als .bak.*)."
fi

# Hub-Section schreiben, sobald ein Pi-Token gesetzt ist (URL optional bei discover).
HUB_BLOCK=""
if [[ -n "${HUB_TOKEN}" ]]; then
    HUB_BASE_TOML="${HUB_URL}"
  if [[ -z "${HUB_BASE_TOML}" ]] || [[ "${HUB_BASE_TOML}" == "auto" ]] || [[ "${HUB_BASE_TOML}" == "discover" ]]; then
        HUB_BASE_TOML="auto"
    fi
    HUB_BLOCK=$(cat <<EOF

[hub]
base_url                      = "${HUB_BASE_TOML}"
discover                      = ${HUB_DISCOVER}
hub_port                      = ${HUB_PORT}
discover_interval_seconds     = ${HUB_DISCOVER_INTERVAL}
pi_token                      = "${HUB_TOKEN}"
heartbeat_interval_seconds    = 5.0
update_check_interval_seconds = 30.0
EOF
)
fi

# Git-Repo-Root für Auto-Updater bestimmen (REPO_DIR ist .../pi-app).
GIT_REPO_DIR="$(cd "${REPO_DIR}/.." && pwd)"
UPDATER_BLOCK=""
if [[ -d "${GIT_REPO_DIR}/.git" ]]; then
    # Verhindert "fatal: detected dubious ownership" beim Updater-Service:
    # Repo wird typischerweise als User pi geklont, der Updater laeuft aber
    # als root. /etc/gitconfig (system-weit) schaltet den Check fuer dieses
    # eine Repo aus. Wir nutzen --system, damit es fuer ALLE User greift,
    # die git auf dem Pi aufrufen (root, pi, hotsport, ...).
    if ! git config --system --get-all safe.directory 2>/dev/null | grep -qx "${GIT_REPO_DIR}"; then
        git config --system --add safe.directory "${GIT_REPO_DIR}" || true
    fi

    UPDATER_BLOCK=$(cat <<EOF

[updater]
enabled                       = true
git_repo                      = "${GIT_REPO_DIR}"
branch                        = "main"
install_script                = "${REPO_DIR}/scripts/install.sh"
check_interval_seconds        = 300.0
health_check_delay_seconds    = 30.0
health_check_timeout_seconds  = 60.0
EOF
)
fi

cat > "${CONFIG_FILE}" <<EOF
# /etc/hotsport-access/config.toml
# Automatisch generiert von install.sh aus pi-app/devices.json.
# Manuelle Änderungen werden beim nächsten install.sh-Lauf gesichert
# (.bak.<timestamp>) und dann überschrieben.

pi_id     = "${PI_ID}"
name      = "${PI_NAME}"
location  = "${PI_LOC}"

state_dir        = "${STATE_DIR}"
health_bind_host = "127.0.0.1"
health_bind_port = 8765
${HUB_BLOCK}
[api]
base_url                  = "${API_BASE_URL}"
bearer_token              = "${API_TOKEN}"
verify_tls                = ${API_VERIFY_TLS}
connect_timeout_seconds   = ${API_CONNECT_TIMEOUT}
request_timeout_seconds   = ${API_REQUEST_TIMEOUT}

[pi]
interface_id        = "${INTERFACE}"
inout               = "${INOUT}"
enabled             = true
relay_pin           = ${RELAY_PIN}
relay_pulse_seconds = ${RELAY_PULSE}
buzzer_pin          = ${BUZZER_PIN}

[pi.reader]
mode         = "${MODE}"
device_path  = "${READER_DEVICE}"
camera_index = ${READER_CAMIDX}
${UPDATER_BLOCK}
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
if [[ "${HOTSPORT_NO_UPDATER_RESTART:-0}" == "1" ]]; then
    echo "    (hotsport-updater bleibt – wird vom laufenden Updater fortgeführt)"
else
    systemctl restart hotsport-updater.service
fi

echo
echo "================================================="
echo " Fertig. Pi '${PI_ID}' ist eingerichtet."
echo "================================================="
echo "    Logs:    journalctl -u hotsport-access -f"
echo "    Updater: journalctl -u hotsport-updater -f"
echo "    Health:  curl -s http://127.0.0.1:8765/health | python3 -m json.tool"
echo
if [[ -n "${HUB_URL}" ]] && [[ -n "${HUB_TOKEN}" ]]; then
    echo "Pi sollte innerhalb von ~5 Sekunden im Hub-Dashboard erscheinen:"
    echo "  ${HUB_URL}"
else
    echo "Standalone-Modus: Pi scant ohne Hub direkt gegen die Binarytec-API."
fi
