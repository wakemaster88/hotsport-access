# Hotsport Access – Pi-App

Schlanke Python-Version der Drehkreuz-Steuerung. Ersetzt das alte
PHP/Apache/MySQL-Setup. Eine Schleife, ein Prozess, eine winzige
Bootstrap-Datei. Alles andere kommt vom Hub.

## Komponenten

- `app/main.py` – Hauptdaemon: liest Scans → fragt Binarytec → schaltet
  Relais/Buzzer. Beendet sich bei Konfig-Wechsel; systemd startet sofort neu.
- `app/api.py` – Binarytec-Client mit Retry und kurzen Timeouts.
- `app/gpio.py` – Relais + Buzzer via gpiozero, **musikalisch optimierte Töne**:
  - `beep_valid` – freundlicher Zwei-Ton (C5 → G5)
  - `beep_invalid` – langes absteigendes Brummen (A4 → A2)
  - `beep_error` – tiefe Drei-Stutter (Netz/API-Fehler)
  - `beep_startup` – C-Dur-Akkord aufsteigend (C5-E5-G5-C6)
  - `beep_offline` – tiefer Doppelton (Hub verloren)
  - `beep_config_applied` – kurzer Klick (Config übernommen)
- `app/readers/` – `keyboard` (mit Auto-Reconnect) / `qr_camera` / `rfid_mfrc522`.
- `app/state.py` – SQLite mit lokaler Scan-Historie + Offline-Puffer + Cleanup.
- `app/sysinfo.py` – sammelt Modell, Kernel, MAC, CPU-Temp, Last, Mem, Disk, Uptime.
- `app/hub_client.py` – Heartbeat + Live-Config-Pull + Scan-Push.
- `app/sdnotify.py` – stdlib-only systemd `READY=1` / `WATCHDOG=1`.
- `app/health.py` – `/health` auf 127.0.0.1:8765.
- `updater/updater.py` – pollt den Hub, lädt neue Releases, swapt atomar, Rollback.

## Bootstrap-Konfiguration auf dem Pi

`/etc/hotsport-access/config.toml` enthält **nur** die Mini-Identität und
Hub-Verbindung. Alles andere (API-URL, Token, GPIO-Pins, Reader-Modus,
interface_id, inout-Richtung) wird im **Dashboard** verwaltet:

```toml
pi_id     = "pi-eingang-nord"
name      = "Eingang Nord"
location  = "hotsport"
state_dir = "/var/lib/hotsport-access"

[hub]
base_url = "auto"              # oder feste URL; "auto" = LAN-Suche
discover = true                # scannt periodisch, bis der Hub erreichbar ist
hub_port = 8000
pi_token = "BITTE-EINTRAGEN"
heartbeat_interval_seconds = 5.0
update_check_interval_seconds = 30.0
```

## Ablauf bei Konfigurationsänderung

1. Operator ändert eine Einstellung im Hub-Dashboard (z.B. `interface_id`).
2. Beim nächsten Heartbeat (≤5 s) liefert der Hub einen neuen
   Config-Fingerprint mit.
3. Der Pi holt die neue Live-Config, cached sie und beendet sich sauber.
4. systemd startet den Daemon innerhalb von ~2 s mit der neuen Konfig neu.
5. Beim Erfolg ertönt der `beep_config_applied`-Klick.

Damit ist das Dashboard die **einzige Quelle der Wahrheit** für alle
funktionalen Einstellungen.

## Erst-Installation

```bash
sudo ./scripts/install.sh keyboard       # oder qr_camera / rfid_mfrc522
```

Das Skript:
1. installiert apt-Pakete (python3-venv, RPi.GPIO, ggf. opencv/spidev)
2. legt `/opt/hotsport-access/{releases,current}` an
3. baut `venv` und installiert Requirements
4. erzeugt `/etc/hotsport-access/config.toml` aus der Vorlage
5. aktiviert `hotsport-access.service` (Type=notify mit WatchdogSec=60s) und
   `hotsport-updater.service`

Danach `nano /etc/hotsport-access/config.toml`, `pi_id`, `hub.base_url`,
`hub.pi_token` setzen, `sudo systemctl restart hotsport-access`.

Im Dashboard:
1. **Globale API-Einstellungen**: Base-URL, Bearer-Token, TLS, Timeouts.
2. Den neu auftauchenden Pi anklicken (Pfeil ▾) und in den **Einstellungen**
   `interface_id`, Richtung (in/out), Reader-Modus etc. eintragen.

Sobald gespeichert, übernimmt der Pi alles automatisch.

## Updaten (im Betrieb)

Nicht über `install.sh`. Stattdessen:

1. `cd pi-app && ./scripts/build-release.sh 2026.05.18-1`
2. ZIP per Dashboard hochladen oder per `scp` ins Hub-Releases-Verzeichnis.
3. Im Dashboard die Soll-Version klicken.
4. Updater zieht das ZIP, prüft SHA-256, swapt den Symlink, restartet,
   rollback bei Health-Fehler.

Pis brauchen dafür **keinen Internetzugang** – nur Verbindung zum Hub.

## Robustheit

- **systemd Watchdog**: Daemon sendet `WATCHDOG=1` nach jedem Scan. Hängt
  er > 60s, killt systemd ihn und startet ihn neu.
- **Hardware-Watchdog**: optional; aktivieren via `dtparam=watchdog=on` in
  `/boot/firmware/config.txt`. Hängt der Pi komplett, rebootet er.
- **Read-only Root**: `raspi-config` → Performance → Overlay FS empfohlen.
  Beschreibbar bleiben nur `/var/lib/hotsport-access` und `/var/log/hotsport-access`.
- **Offline-Puffer**: Scans werden lokal gespeichert und beim nächsten
  Heartbeat an den Hub gepusht. Auto-Cleanup nach 30 Tagen.
- **Reader-Reconnect**: Tastatur-Reader (USB-Wedge) baut die Verbindung
  bei Trennung automatisch wieder auf.
- **Live-Config-Cache**: Daemon arbeitet auch ohne Hub-Verbindung weiter,
  solange er die Konfig schon einmal gesehen hat.
