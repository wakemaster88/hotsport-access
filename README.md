# Hotsport Access

Software für die Hotsport-Drehkreuz-Pis (Raspberry Pi Access Controller).

```
hub/        Lokales Status-, Update- und Konfigurations-Dashboard (FastAPI)
pi-app/     Software, die auf jedem Drehkreuz-Pi läuft (Daemon + Updater)
```

## Architektur

```
┌────────────────────────────────────────┐         ┌──────────────────────┐
│  Hub  (z.B. Mac/Pi im LAN)             │         │  Binarytec-API       │
│  - Dashboard (Status + Konfiguration)  │         │  192.168.251.50:444  │
│  - Release-Hosting                     │         └──────────────────────┘
│  - SQLite-Statusdatenbank              │                    ▲
└──────┬─────────────────────────────────┘                    │
       │ Heartbeat ↑ / Live-Config ↓                          │
       ▼                                                      │
┌────────────────────────────────────────┐                    │
│  Pi #1 ... Pi #N                       │ Scan → check-access│
│  - hotsport-access.service             │────────────────────┘
│  - hotsport-updater.service            │
└────────────────────────────────────────┘
```

- Pis brauchen Internet **nur einmalig beim Bootstrap** (~30 MB apt + pip).
  Im Live-Betrieb reicht reines LAN.
- Pis kennen genau zwei Adressen: den Hub und die Binarytec-API.
- **Konfiguration komplett im Hub-Dashboard**. Pis halten nur eine winzige
  Bootstrap-Datei (`pi_id` + Hub-URL + Hub-Token).
- Updates rollt man per Klick im Dashboard aus. Auto-Rollback bei Fehler.

## Schnellstart

### 1. Hub aufsetzen (einmalig)

```bash
git clone https://github.com/wakemaster88/hotsport-access.git
cd hotsport-access/hub

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Pi-Token erzeugen und in /etc/hotsport-hub.env eintragen
openssl rand -hex 32
sudoedit /etc/hotsport-hub.env
# HOTSPORT_HUB_PI_TOKEN=<token>
# HOTSPORT_HUB_PUBLIC_URL=http://<lan-ip>:8000
# HOTSPORT_HUB_DASHBOARD_USER=admin
# HOTSPORT_HUB_DASHBOARD_PASSWORD=<starkes-passwort>

# Hub starten (für systemd siehe hub/systemd/)
sudo systemctl enable --now hotsport-hub
```

Dashboard danach unter `http://<hub-ip>:8000/` öffnen.
Setup-Anleitung mit Schritt-für-Schritt-Installation: `http://<hub-ip>:8000/setup`.

### 2. Pi-App auf einem Drehkreuz-Pi (für jeden Pi)

Auf dem Pi (mit Internetzugang fürs Bootstrap):

```bash
curl -fsSL http://<hub-ip>:8000/install.sh | sudo bash
```

Der Installer fragt interaktiv nach Token, Pi-ID, Name, Standort und
Reader-Modus (`keyboard` / `qr_camera` / `rfid_mfrc522`). Token findest du
auf der Setup-Seite des Hubs (`/setup`).

Pi taucht im Dashboard auf. Dort über das Detail-Panel die Felder
`interface_id`, `inout`, GPIO-Pins etc. setzen. Sobald gespeichert,
übernimmt der Pi die neue Config beim nächsten Heartbeat (≤ 5 s).

Nach dem Bootstrap kann der Pi dauerhaft offline (LAN-only) laufen.

### 3. Globale API-Einstellungen im Dashboard

Im Dashboard → „Globale API-Einstellungen":

| Feld          | Wert                                           |
| ------------- | ---------------------------------------------- |
| Base-URL      | `https://192.168.251.50:444`                   |
| Bearer-Token  | (das Binarytec-Token)                          |
| TLS prüfen    | „nein (selbstsigniert)" oder Pfad zum CA-PEM   |

### 4. Release bauen & ausrollen

```bash
cd pi-app
./scripts/build-release.sh 2026.05.18-1
# erzeugt dist/hotsport-access-2026.05.18-1.zip + .sha256

# Release zum Hub kopieren (dort liegt das Releases-Verzeichnis):
scp dist/hotsport-access-2026.05.18-1.zip hub:/var/lib/hotsport-hub/releases/
scp dist/hotsport-access-2026.05.18-1.zip.sha256 hub:/var/lib/hotsport-hub/releases/
```

Im Dashboard die Version auswählen und „Setzen" klicken. Pis ziehen das
Update beim nächsten Heartbeat, validieren die SHA-256 und switchen den
`current`-Symlink atomar. Bei Fehler automatischer Rollback auf
`previous`.

## Buzzer-Töne

Audio-Signale, klar unterscheidbar auch in lauter Umgebung:

| Ereignis              | Klang                                         |
| --------------------- | --------------------------------------------- |
| **Zugang erlaubt**    | Aufsteigender Zwei-Ton (C5 → G5)              |
| **Zugang verwehrt**   | Langes absteigendes Brummen (A4 → A2)         |
| **API/Netzfehler**    | 3× kurzes Stuttern (E3)                       |
| **Hub verloren**      | Tiefer Doppelton (C3)                         |
| **Pi deaktiviert**    | Weicher F4-„Klopf-Klopf"                      |
| **Systemstart**       | C-Dur-Akkord aufsteigend (C5-E5-G5-C6)        |
| **Konfig übernommen** | Kurzer Klick (G5, 50 ms)                      |

## Verzeichnisstruktur

```
hub/
  app/               FastAPI-Anwendung (DB, Routen, Auth, Dashboard)
  templates/         Jinja-Templates
  static/            CSS, install.sh (Bootstrap-Script)
  systemd/           hotsport-hub.service + .env-Beispiel
pi-app/
  app/               Daemon: Reader → Binarytec-API → GPIO,
                     Hub-Client, Heartbeat, Sysinfo, sd_notify
  app/readers/       keyboard / qr_camera / rfid_mfrc522
  updater/           Eigenständiger Updater-Service
  config/            Bootstrap-config.example.toml
  systemd/           hotsport-access.service + hotsport-updater.service
  scripts/           install.sh + build-release.sh
```

## Sicherheitshinweise

- **Pi-Token** (`HOTSPORT_HUB_PI_TOKEN`): wird auf jedem Pi gespeichert.
  Bei Verlust eines Pis Token rotieren (Hub + alle anderen Pis updaten).
- **Dashboard-Auth**: in Produktion immer `HOTSPORT_HUB_DASHBOARD_USER` +
  `HOTSPORT_HUB_DASHBOARD_PASSWORD` setzen.
- **Binarytec-API-Bearer**: wird im Hub-Dashboard verschlüsselt
  gespeichert und nur an die berechtigten Pis ausgeliefert.

## Lizenz

Internes Projekt. Kein offener Lizenzeintrag.
