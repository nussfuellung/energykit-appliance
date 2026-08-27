# EnergyKit Appliance v0.4 Alpha

EnergyKit ist eine HAOS-basierte Energie-Appliance mit geführtem Web-Wizard für Sigenergy/Deye, evcc, Wallbox, Wärmepumpe, Dashboard, Diagnose und Service.

## Was v0.4 gegenüber v0.3 ergänzt

Der komplette Inbetriebnahmezyklus kann in **Simulation** auf einer HAOS-VM durchgespielt werden:

1. EnergyKit ISO installiert unverändertes offizielles HAOS.
2. EnergyKit wird als lokale HAOS-App in `hassos-data` vorgemerkt.
3. Nach dem HA-Owner-Onboarding startet der EnergyKit-Ingress-Wizard.
4. Service-Benutzer `energykit-service` wird in Home Assistant erzeugt.
5. Kunden-/Anlagendaten werden gespeichert.
6. Geräte können simuliert oder im LAN gesucht werden.
7. Mushroom, VisionOS, Sigenergy, Deye und EnergyKit Bridge werden ohne HACS verwaltet.
8. Sigenergy-/Deye-Config-Flows können direkt aus EnergyKit durchgereicht werden.
9. EnergyKit ordnet Quell-Entities automatisch den normalisierten `sensor.ek_*` Sensoren zu; unsichere Treffer können manuell korrigiert werden.
10. Die EnergyKit Bridge erzeugt die einheitlichen PV-/Haus-/Netz-/Batterie-Sensoren und kann SG-Ready anhand von Netzexport steuern.
11. Die offizielle evcc HAOS-App wird installiert und eine `evcc.yaml` aus den EnergyKit-Sensoren erzeugt.
12. Wallbox und Wärmepumpe werden im Wizard konfiguriert.
13. Ein Mushroom-basiertes EnergyKit Dashboard wird angelegt.
14. Eine End-to-End-Prüfung sperrt die Übergabe, solange Pflichtprüfungen fehlschlagen.
15. Übergabebericht kann als HTML oder JSON exportiert werden.
16. Vor Übergabe wird ein Supervisor-Backup versucht.
17. Nach Übergabe darf ausschließlich die gespeicherte HA-User-ID des Service-Benutzers EnergyKit öffnen.
18. Service-Modus bietet Diagnose, Komponentenupdates, Backup, Berichte und Recovery in den Setup-Modus.

## Bewusst noch Alpha

- Update-Härtung (gepinntes Release-Manifest, SHA-256 pro Drittkomponente, automatischer Rollback) ist noch **nicht** fertig.
- Sigenergy/Deye werden im Echtbetrieb nicht anhand erfundener Register erkannt. EnergyKit installiert die Integrationen und nutzt ihre echten HA Config-Flows. Das Entity-Mapping ist heuristisch und fällt bei Unsicherheit auf manuelle Auswahl zurück.
- Wallboxen werden für v0.4 über eine HA-Switch-Entity an evcc übergeben. Herstellerspezifische evcc-Charger-Templates kommen danach.
- `apps.json`-Preseed ist eine versiongebundene Appliance-Technik und keine garantierte öffentliche HAOS-OEM-API. Jede neue HAOS/Supervisor-Version muss vor Freigabe getestet werden.

## VM-Test

Empfohlen:

- UEFI VM
- 2 bis 4 vCPU
- 4 GB RAM
- 40 GB virtuelle Systemdisk
- zusätzliche virtuelle CD/DVD mit dem EnergyKit ISO
- NAT reicht für Simulation; Bridge Networking für echte LAN-Geräte

### GitHub Release bauen

Repository hochladen und taggen:

```bash
git init
git add .
git commit -m "EnergyKit Appliance v0.4 alpha"
git branch -M main
git remote add origin https://github.com/DEIN-USER/energykit-appliance.git
git push -u origin main

git tag v0.4.0
git push origin v0.4.0
```

Die GitHub Action baut:

- `ghcr.io/<owner>/energykit-amd64:0.4.0`
- `energykit-installer-amd64.iso`
- `energykit-installer-amd64.iso.sha256`

Das GHCR-Paket muss öffentlich lesbar sein, damit eine neue Appliance das EnergyKit-Image beim ersten Start laden kann.

## Testablauf Simulation

1. ISO booten und virtuelle SSD auswählen.
2. `INSTALLIEREN` bestätigen.
3. Nach Abschluss ISO entfernen und HAOS booten.
4. `http://homeassistant.local:8123` öffnen und HA-Owner anlegen.
5. EnergyKit über Home Assistant öffnen.
6. **Simulation** wählen.
7. Anlagendaten speichern.
8. `energykit-service` erzeugen und Zugangsdaten herunterladen.
9. Simuliertes Sigenergy auswählen.
10. Basis-Komponenten installieren.
11. `Automatisch zuordnen` bei EnergyKit Messwerten und Mapping speichern. HA Core startet dabei neu und die EnergyKit Bridge wird eingerichtet.
12. Optional Wallbox auf `Home-Assistant Switch` und `switch.wallbox_enable` setzen.
13. Optional Wärmepumpe auf SG-Ready mit `switch.sg_ready` setzen.
14. evcc installieren und anschließend Konfiguration erzeugen.
15. EnergyKit Dashboard erzeugen.
16. Abschlussprüfung starten. Für die Übergabe müssen alle Pflichtprüfungen grün sein.
17. HTML-/JSON-Bericht herunterladen und Abschluss-Backup erzeugen.
18. `Anlage übergeben` klicken.
19. Owner erneut EnergyKit öffnen: Zugriff muss verweigert werden.
20. Als `energykit-service` anmelden: EnergyKit Service-Modus muss zugänglich sein.

## Realbetrieb

In Echtbetrieb durchsucht EnergyKit das lokale /24 auf typische Ports. Sigenergy und Deye werden anschließend über die von EnergyKit installierten Home-Assistant-Custom-Integrationen eingerichtet. Die originalen Config-Flow-Schemas werden dynamisch gerendert, damit Änderungen an den Integrationen nicht als fest verdrahtete EnergyKit-Formulare enden.

## evcc

EnergyKit installiert die offizielle evcc HAOS-App und schreibt deren `evcc.yaml` in das öffentliche App-Konfigurationsverzeichnis. Die EnergyKit-Meter werden als Home-Assistant-Template-Meter an evcc übergeben:

- `sensor.ek_grid_power`
- `sensor.ek_pv_power`
- `sensor.ek_battery_power`
- `sensor.ek_battery_soc`

Home Assistant und evcc selbst werden weiterhin über ihre eigenen Updatepfade aktualisiert.

## Security-Modell

Während Setup ist die EnergyKit-Seitenleiste durch `panel_admin: true` auf HA-Administratoren beschränkt. Nach Übergabe prüft EnergyKit zusätzlich `X-Remote-User-Id` gegen die beim Provisionieren gespeicherte Service-User-ID. Das Service-Passwort wird nach erfolgreicher Übergabe aus dem EnergyKit-State gelöscht.

## Verzeichnisstruktur

```text
energykit/
  config.yaml
  Dockerfile
  app/
    main.py
    bundled/energykit_bridge/
installer/
  build.sh
  config/
  preload/
.github/workflows/
scripts/
```

## Lizenz / Produktbetrieb

Vor kommerziellem Rollout müssen Lizenzen der eingebundenen Drittkomponenten, Security-Härtung, Release-Pinning und die HAOS/Supervisor-Kompatibilität je Appliance-Release geprüft werden.


## Erster vollständiger VM-Test

Siehe:

- `docs/GITHUB-QUICKSTART.md`
- `docs/VM-FIRST-BOOT.md`
- `docs/TESTPLAN.md`

v0.4.1 fügt außerdem nach dem ISO-Build einen formalen Artifact-Check inklusive SHA-256-Verifikation hinzu. Das ersetzt keinen echten UEFI-Boot-Test.


## v0.5.0 Wizard Alpha

- geführter 9-Schritt-Inbetriebnahme-Wizard statt One-Pager
- eigene Modals, Toasts, Lade- und Fortschrittszustände
- Diagnoseansicht für Supervisor/Core/WebSocket
- korrigierte Config-Flow-Domains (`sigen`, `deye_modbus`)
- idempotenter Dashboard-Pfad `energykit-dashboard`
- HAOS-Preseed legt `apps/data/local_energykit` an
- Installer setzt Execute-Bit explizit
- deutsches Konsolenlayout im Installer
- Netzwerk-/DHCP-Preflight vor HAOS-Download


## v0.5.1 – GRUB Theme + Graphical Appliance Installer

- eigenes EnergyKit GRUB Theme für UEFI/GRUB mit H-IT Branding
- klare Boot-Einträge: EnergyKit installieren, Recovery, Systeminformationen, Ausschalten
- lokaler grafischer Tk/X11-Installer statt tty-Onepager
- gleiche dunkle Appliance-Designsprache wie EnergyKit in Home Assistant
- Laufwerkskarten, Löschbestätigung, Netzwerkstatus, Download-Fortschritt, Fehlerseite und Abschlussseite
- HAOS-/Preseed-Logik unverändert; kein Plymouth und kein geänderter HAOS-Bootpfad


## v0.5.2 – API/Auth Fixes

- Preseed hinterlegt den Supervisor-Token zusätzlich als `ENERGYKIT_SUPERVISOR_TOKEN`
- EnergyKit nutzt diesen Token als Fallback, falls `SUPERVISOR_TOKEN` beim ersten Start fehlt
- `all_addon_configs` auf `all_app_configs` umgestellt
- VisionOS wird aus dem aktuellen GitHub Release geladen
- Sigenergy, Deye und EnergyKit Bridge markieren einen notwendigen HA-Neustart
- Config Flows werden bis zum erforderlichen Neustart blockiert
- Restart-Endpunkt wartet auf die Rückkehr von Home Assistant Core
- Diagnose zeigt jetzt die Token-Quelle
- Dashboard prüft Mushroom und ausstehende Neustarts


## v0.5.3 – UI / UEFI Boot Fix

- grafischer Installer responsiver und kompakter
- kleineres H-IT-Logo
- erste Zieldisk wird automatisch ausgewählt
- komplette Laufwerkskarte klickbar
- Enter/↑/↓ als Tastatur-Fallback
- expliziter libinput/QXL/SPICE-Input-Stack für virt-manager
- GRUB-Theme jetzt für `grub-pc` **und** `grub-efi`
- Binary-Hook injiziert Theme auch in von live-build generierte UEFI-GRUB-Konfigurationen


## ISO UEFI sanity-check fix

`live-build` kann den UEFI-Bootloader in einem eingebetteten EFI-/El-Torito-Image
ablegen. Der Build verlangt `EFI/BOOT/BOOTX64.EFI` deshalb nicht mehr als
sichtbare ISO9660-Datei. Stattdessen wird die El-Torito-/UEFI-Bootstruktur mit
`xorriso -report_el_torito` geprüft. Kernel und initrd bleiben harte Checks.


## ISO sanity check v3

Kernel- und initrd-Pfade sind nur noch diagnostisch. Harte Build-Abbrüche
erfolgen nur noch, wenn keine UEFI-/El-Torito-Bootstruktur erkannt wird.
Zusätzlich werden `/live`, sichtbare GRUB-/EFI-Dateien und der ISO-Root im
GitHub-Log ausgegeben, damit unterschiedliche live-build-Strukturen direkt
sichtbar sind.
