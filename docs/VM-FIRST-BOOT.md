# VM First-Boot Test & Fehlersuche

## Gate A – ISO

Erwartung:

- UEFI bootet das ISO.
- Installer erscheint auf tty1.
- Ziel-SSD wird korrekt erkannt.
- HAOS-Download und SHA-256-Prüfung funktionieren.
- `hassos-data` wird nach dem Flashen gefunden.
- EnergyKit-Preseed wird ohne Fehler geschrieben.

Falls der Installer stoppt, fotografiere/kope die komplette Fehlermeldung. Noch nicht erneut installieren, bevor klar ist, an welchem Gate es scheitert.

## Gate B – HAOS

Nach Entfernen des ISO muss die VM von der SSD booten.

Erwartung:

- HAOS startet.
- DHCP-Adresse vorhanden.
- `homeassistant.local:8123` bzw. die VM-IP wird erreichbar.

Der allererste Start kann deutlich länger dauern, weil Images heruntergeladen werden.

## Gate C – Supervisor erkennt EnergyKit

Nach Owner-Onboarding:

`Einstellungen → Apps`

Erwartung:

- lokale App `EnergyKit`
- interner Slug: `local_energykit`
- Autostart aktiviert

Wenn die App vorhanden, aber nicht gestartet ist:

- App-Protokoll öffnen.
- Prüfen, ob das GHCR-Image erreichbar ist.
- Prüfen, ob das GitHub Package öffentlich lesbar ist.

Wenn EnergyKit überhaupt nicht im App Store/bei Apps erscheint, ist der Preseed unser erster Verdächtiger. Dann brauchen wir Supervisor-Logs und die genaue HAOS/Supervisor-Version.

## Gate D – Ingress

EnergyKit öffnen.

Während Setup:

- Owner/Admin darf öffnen.
- `X-Remote-User-Id` muss bei EnergyKit ankommen.

Nach Übergabe:

- Owner wird von EnergyKit abgewiesen.
- ausschließlich die gespeicherte HA-User-ID von `energykit-service` wird akzeptiert.

## Gate E – Simulation

Simulation wählen und vollständig durchlaufen:

- simuliertes Sigenergy
- PV / Haus / Netz / Batterie / SoC
- EnergyKit Bridge
- `sensor.ek_*`
- Mushroom Dashboard
- optional Wallbox
- optional SG-Ready
- evcc
- Abschlussprüfung
- Übergabebericht
- Backup
- Übergabe

## Was du bei einem Fehler sichern solltest

Mindestens:

- Screenshot der Fehlermeldung
- HAOS-Version
- Supervisor-Version
- Home-Assistant-Version
- EnergyKit-App-Log
- Supervisor-Log rund um `local_energykit`
- bei Installerfehlern die letzte komplette Bildschirmseite

Damit können wir den nächsten Fix sehr gezielt bauen.
