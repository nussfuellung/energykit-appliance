# GitHub → ISO in wenigen Minuten

## 1. Repository erstellen

Erstelle auf GitHub ein leeres Repository, z. B.:

`energykit-appliance`

Dann lokal im entpackten Projekt:

```bash
git init
git add .
git commit -m "EnergyKit Appliance v0.4.1 alpha"
git branch -M main
git remote add origin https://github.com/DEIN-USER/energykit-appliance.git
git push -u origin main
```

## 2. GHCR-Paket beachten

Der Workflow baut das EnergyKit-App-Image nach:

`ghcr.io/<github-user>/energykit-amd64:<version>`

Nach dem ersten erfolgreichen Build auf GitHub unter **Packages** prüfen, dass das Paket von HAOS ohne private Registry-Credentials gelesen werden kann. Für den Appliance-Test sollte es öffentlich lesbar sein.

## 3. Release bauen

```bash
git tag v0.4.1
git push origin v0.4.1
```

GitHub Actions führt danach `Build EnergyKit Appliance` aus.

Erwartete Artefakte:

- `energykit-installer-amd64.iso`
- `energykit-installer-amd64.iso.sha256`

Bei einem Tag werden sie zusätzlich an den GitHub Release angehängt.

## 4. VM anlegen

Empfohlen für den ersten Test:

- UEFI
- x86-64
- 2–4 vCPU
- 4 GB RAM
- 32 GB virtuelle SSD
- zweite CD/DVD mit dem EnergyKit ISO
- Netzwerk zunächst NAT für Simulation
- Secure Boot aus

Für späteren Hardware-Discovery-Test Netzwerk auf Bridge umstellen.

## 5. ISO booten

Vom EnergyKit-ISO starten.

Der Installer muss:

1. die virtuelle SSD anbieten,
2. das USB/CD-Installationsmedium nicht als Ziel anbieten,
3. erst nach Eingabe von `INSTALLIEREN` fortfahren,
4. offizielles HAOS herunterladen,
5. SHA-256 verifizieren,
6. HAOS auf die virtuelle SSD schreiben,
7. `hassos-data` finden,
8. `local_energykit` preseeden,
9. herunterfahren.

Danach ISO auswerfen und von der virtuellen SSD booten.

## 6. Erwarteter First Boot

Nach einigen Minuten:

`http://homeassistant.local:8123`

Owner-Onboarding durchführen.

Danach prüfen:

**Einstellungen → Apps**

Erwartung:

- EnergyKit ist bereits vorhanden.
- EnergyKit steht auf Autostart.
- Das App-Image wird geladen bzw. ist bereits geladen.
- Nach Start erscheint das EnergyKit-Panel.

Danach Simulation komplett durchspielen.

## 7. Wichtigster Rechte-Test

Am Ende des Wizards:

1. Service-Zugangsdaten speichern.
2. Übergabe abschließen.
3. EnergyKit als Owner öffnen → Zugriff muss verweigert werden.
4. Als `energykit-service` anmelden → Zugriff muss funktionieren.

Wenn dieser Test funktioniert, ist der Appliance-/Service-Auth-Kreislauf validiert.
