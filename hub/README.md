# Hotsport Access Hub

Lokales Status-, Update- und **Konfigurationszentrum** für die Drehkreuz-Pis.

## Was er macht

- Zeigt im Browser eine Live-Übersicht aller Drehkreuze inkl.
  **Systeminformationen** (CPU-Temperatur, Last, Memory, Datenträger,
  Uptime, Modell, Kernel, MAC).
- Hostet die Release-ZIPs samt SHA-256-Prüfsummen und rollt sie per
  Klick aus.
- **Verwaltet die globalen API-Einstellungen** (Binarytec Base-URL,
  Bearer-Token, TLS, Timeouts) zentral.
- **Konfiguriert jeden Pi einzeln** (interface_id, in/out-Richtung,
  Reader-Modus, GPIO-Pins für Relais & Buzzer, Notizen).
- Empfängt Heartbeats und Scan-Events der Pis und legt sie in einer
  SQLite ab.

Kein Cloud-Anteil. Kein Internetzugang notwendig (nach Erst-Installation).

## Hardware

Ein beliebiger Pi 4/5 oder Mini-PC im LAN. Anforderungen sind minimal:
~50 MB RAM, ca. 200 MB Disk für die App, plus Platz für Releases.

## Erst-Installation

```bash
sudo ./scripts/install.sh
```

Das Skript:
1. legt einen Service-User `hotsport` an
2. installiert Abhängigkeiten in `/opt/hotsport-hub/venv`
3. legt `/etc/hotsport-hub/hub.env` an (Beispielwerte – **anpassen!**)
4. aktiviert `systemctl enable --now hotsport-hub.service`

Danach erreichbar unter `http://<hub-ip>:8000/`.

## Workflow im Dashboard

1. **Globale API-Einstellungen** (oberste Karte):
   Base-URL, Bearer-Token, TLS-Einstellung, Timeouts. Token wird
   maskiert; nur ein neuer Wert überschreibt das gespeicherte Token.
2. **Drehkreuze**: jede Zeile hat einen ▾-Toggle, der ein Detail-Panel öffnet:
   - **Systeminformationen** (read-only): Modell, IP, MAC, CPU-Temp,
     Speicher, Datenträger, Uptime, aktuelle/Soll-Version.
   - **Einstellungen** (Form): Anzeigename, Standort, interface_id,
     Richtung in/out, Reader-Modus + Devicepfad/Kameraindex, GPIO-Pins
     für Relais & Buzzer, Pulsdauer, Notizen.
3. Speichern → Pi übernimmt die Änderung beim nächsten Heartbeat
   (innerhalb weniger Sekunden) automatisch und startet sich kurz neu.
4. **Releases**: ZIPs hochladen und per Klick je Pi (oder „Auf alle anwenden")
   ausrollen.

## Releases hochladen

Zwei Wege:

### Per Dashboard

Im Bereich „Releases" → „Neuen Release hochladen" → ZIP wählen → Version
eintragen. Der Hub erzeugt automatisch die SHA-256-Begleitdatei.

### Per scp

```bash
scp dist/hotsport-access-2026.05.18-1.zip hotsport@hub.local:/var/lib/hotsport-hub/releases/
```

Erwarteter Dateiname: `hotsport-access-<version>.zip`.

## Sicherheit

- Dashboard und Admin-Endpunkte sind per Basic-Auth geschützt
  (`HOTSPORT_HUB_DASHBOARD_USER` / `…_PASSWORD`).
- Pi-Endpunkte (Heartbeat / Scan / Config / Desired) erwarten ein
  statisches Bearer-Token (`HOTSPORT_HUB_PI_TOKEN`). Dasselbe Token
  muss in der Pi-Bootstrap-Config eingetragen sein.
- Releases unter `/releases/<file>` sind absichtlich offen, damit der
  Pi-Updater sie ohne Auth ziehen kann. Schutz erfolgt über
  SHA-256-Pflicht im Pi.
- Die im Klartext gespeicherten Token (Hub-DB, Pi-Config) sind nur via
  Basic-Auth bzw. Datei-Permissions zugänglich. Empfehlung:
  Hub auf eigenes VLAN/Subnetz beschränken.

## API-Endpunkte (Pi → Hub)

Alle erwarten `Authorization: Bearer <HOTSPORT_HUB_PI_TOKEN>`.

| Methode | Pfad                      | Zweck                                        |
| ------- | ------------------------- | -------------------------------------------- |
| POST    | `/api/heartbeat`          | Pi meldet Status + Systeminfo                |
| POST    | `/api/scan`               | Pi schickt jeden Scan zur Übersicht          |
| GET     | `/api/config/{pi_id}`     | Live-Konfiguration (API + Pi-Settings)       |
| GET     | `/api/desired/{pi_id}`    | Soll-Version inkl. Download-URL              |
| GET     | `/releases/{filename}`    | Statisches Release-ZIP (kein Token)          |

## Datenbank-Schema

SQLite, zwei Tabellen plus Settings & Audit:

- `pis` – Stammdaten + Live-Settings + Systeminfo aus Heartbeats.
- `scans` – Scan-Historie aller Pis.
- `settings` – globale Key/Value-Settings (z.B. `api.base_url`).
- `audit` – Änderungs-Log (wer hat wann was gesetzt).

Migrationen sind **additiv**: bei Hub-Updates werden fehlende Spalten
automatisch per `ALTER TABLE` ergänzt.
