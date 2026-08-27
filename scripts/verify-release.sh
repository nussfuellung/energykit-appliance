#!/usr/bin/env bash
set -Eeuo pipefail

ISO="${1:-energykit-installer-amd64.iso}"
SUM="${2:-energykit-installer-amd64.iso.sha256}"

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
