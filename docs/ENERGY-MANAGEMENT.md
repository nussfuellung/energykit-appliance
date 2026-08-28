# EnergyKit Energiemanagement

## Zuständigkeiten

EnergyKit ist Provisionierung, Gerätesuche, Normalisierung und Kundenoberfläche.
evcc ist die einzige Laufzeit-Regelungsengine für Wallboxen und Wärmepumpen.
Home Assistant visualisiert und stellt bei nicht nativ unterstützten Geräten optional einen Fallback bereit.

Priorität für Verbraucher: `evcc native` → `direktes Modbus/API-Gerät` → `Home Assistant Fallback`.

Die `energykit_bridge` enthält absichtlich keine SG-Ready-Automation.
