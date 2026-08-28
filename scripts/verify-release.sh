#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ISO="${1:-$PROJECT_ROOT/energykit-installer-amd64.iso}"
SUM="${2:-$PROJECT_ROOT/energykit-installer-amd64.iso.sha256}"

[[ -s "$ISO" ]] || { echo "ISO fehlt oder ist leer: $ISO"; exit 2; }
[[ -s "$SUM" ]] || { echo "SHA256-Datei fehlt: $SUM"; exit 2; }

echo "== Prüfsumme =="
sha256sum -c "$SUM"

echo
echo "== Dateityp =="
file "$ISO"

echo
echo "== Größe =="
du -h "$ISO"

echo
echo "ISO-Artefakt sieht formal plausibel aus."
echo "Der echte Gate-Test bleibt: UEFI-VM booten und HAOS installieren."


echo
echo "== Architektur-Guardrails =="
MAIN_PY="$PROJECT_ROOT/energykit/app/main.py"
BRIDGE_DIR="$PROJECT_ROOT/energykit/app/bundled/energykit_bridge"

[[ -f "$MAIN_PY" ]] || { echo "EnergyKit main.py fehlt: $MAIN_PY"; exit 2; }

if grep -R -q '_setup_heatpump_control\|power_threshold\|off_threshold\|delay_min' "$BRIDGE_DIR" 2>/dev/null; then
  echo 'FEHLER: EnergyKit Bridge enthält wieder eigene Wärmepumpen-Regelung.'
  exit 1
fi

grep -q 'sigenergy-evdc' "$MAIN_PY" || { echo 'FEHLER: Sigenergy EVDC evcc-Treiber fehlt.'; exit 1; }
grep -q 'deye-hybrid-3p' "$MAIN_PY" || { echo 'FEHLER: Deye Hybrid evcc-Treiber fehlt.'; exit 1; }

echo 'EnergyKit Architektur-Guardrails: OK'
