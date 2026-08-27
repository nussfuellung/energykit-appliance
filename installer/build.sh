#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

command -v lb >/dev/null || {
  echo "FEHLER: live-build fehlt."
  echo "Debian/Ubuntu: sudo apt install live-build"
  exit 1
}

echo "======================================"
echo " EnergyKit ISO Build"
echo "======================================"
echo

echo "[1/4] Alten Build bereinigen..."
rm -rf \
  .build-cache \
  binary* \
  chroot* \
  cache \
  local \
  .build

echo
echo "[2/4] live-build konfigurieren..."
bash ./auto/config

echo
echo "== Prüfe Debian Security Quellen =="

grep -R "bookworm/updates" -n . || true
grep -R "bookworm-security" -n . || true

echo
echo "[3/4] ISO bauen..."
lb build

echo
echo "[4/4] ISO-Artefakt vorbereiten..."

ISO="$(find . \
  -maxdepth 1 \
  -type f \
  -name 'live-image-amd64.hybrid.iso' \
  -print \
  -quit)"

if [[ -z "$ISO" ]]; then
  echo "FEHLER:"
  echo "live-build wurde beendet, aber"
  echo "live-image-amd64.hybrid.iso wurde nicht gefunden."
  exit 1
fi

echo
echo "== Prüfe UEFI/GRUB-Struktur im fertigen ISO =="
command -v xorriso >/dev/null || { echo "FEHLER: xorriso fehlt."; exit 1; }
ISO_LIST="$(xorriso -indev "$ISO" -find / -type f -print 2>/dev/null || true)"
printf '%s\n' "$ISO_LIST" | grep -Eqi '/EFI/BOOT/BOOTX64\.EFI$' || {
  echo "FEHLER: EFI/BOOT/BOOTX64.EFI fehlt im ISO."; exit 1;
}
printf '%s\n' "$ISO_LIST" | grep -Eqi '/boot/grub/config\.cfg$' || {
  echo "FEHLER: /boot/grub/config.cfg fehlt im ISO."; exit 1;
}
printf '%s\n' "$ISO_LIST" | grep -Eqi '/boot/grub/live-theme/theme\.txt$' || {
  echo "FEHLER: EnergyKit GRUB Theme fehlt im ISO."; exit 1;
}
printf '%s\n' "$ISO_LIST" | grep -Eqi '/live/vmlinuz' || {
  echo "FEHLER: Live-Kernel fehlt im ISO."; exit 1;
}
printf '%s\n' "$ISO_LIST" | grep -Eqi '/live/initrd' || {
  echo "FEHLER: Live-initrd fehlt im ISO."; exit 1;
}
echo "UEFI/GRUB-Struktur: OK"

OUTPUT_ISO="../energykit-installer-amd64.iso"
OUTPUT_SHA="../energykit-installer-amd64.iso.sha256"

rm -f "$OUTPUT_ISO" "$OUTPUT_SHA"

mv "$ISO" "$OUTPUT_ISO"

cd ..

echo
echo "Erzeuge SHA-256..."

sha256sum energykit-installer-amd64.iso \
  > energykit-installer-amd64.iso.sha256

echo
echo "Prüfe SHA-256 direkt..."

sha256sum -c energykit-installer-amd64.iso.sha256

echo
echo "======================================"
echo " EnergyKit ISO erfolgreich erstellt"
echo "======================================"
echo

ls -lh energykit-installer-amd64.iso
ls -lh energykit-installer-amd64.iso.sha256

echo
echo "SHA-256:"
cat energykit-installer-amd64.iso.sha256