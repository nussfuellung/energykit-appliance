# EnergyKit / evcc Architektur

EnergyKit ist Provisionierung, Wizard, Dashboard und Integrationsschicht.
evcc ist die einzige Energiemanagement-Engine.

Priorität der Geräteanbindung:

1. nativer evcc-Treiber
2. direkte Modbus/API-Anbindung
3. Home-Assistant-Entity nur als Fallback

Für Sigenergy gilt:
- Sigen Hybrid / Plant: Grid, PV und Batterie direkt via evcc Modbus
- Sigen EVAC / EVDC: direkt via evcc
- Home Assistant `sensor.ek_*`: Dashboard/Diagnose/Fallback

Damit bleibt die Regelung auch dann stabil, wenn sich HA-Entities ändern.
