#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

command -v lb >/dev/null || {
  echo "live-build fehlt. Debian/Ubuntu: sudo apt install live-build"
  exit 1
}

rm -rf .build-cache binary* chroot* cache local .build

echo "== live-build konfigurieren =="
bash ./auto/config

echo
echo "== Prüfe Debian Security Quellen =="
grep -R "bookworm/updates" -n . || true
grep -R "bookworm-security" -n . || true

echo
echo "== ISO bauen =="
lb build

ISO="$(find . -maxdepth 1 -name 'live-image-amd64.hybrid.iso' -print -quit)"

if [[ -z "$ISO" ]]; then
  echo "FEHLER: live-build wurde beendet, aber kein ISO gefunden."
  exit 1
fi

mv "$ISO" "../energykit-installer-amd64.iso"

sha256sum ../energykit-installer-amd64.iso \
  > ../energykit-installer-amd64.iso.sha256

echo
echo "Fertig:"
ls -lh ../energykit-installer-amd64.iso
cat ../energykit-installer-amd64.iso.sha256
