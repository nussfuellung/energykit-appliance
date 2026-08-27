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
echo "== Prüfe Boot-Struktur im fertigen ISO =="
command -v xorriso >/dev/null || { echo "FEHLER: xorriso fehlt."; exit 1; }

echo
echo "== El-Torito / UEFI Report =="
REPORT="$(xorriso -indev "$ISO" -report_el_torito as_mkisofs 2>/dev/null || true)"
printf '%s\n' "$REPORT"

# Bei live-build liegt BOOTX64.EFI häufig in einem eingebetteten EFI-/El-Torito-Image
# und ist deshalb nicht als /EFI/BOOT/BOOTX64.EFI im ISO9660-Dateibaum sichtbar.
if ! printf '%s\n' "$REPORT" | grep -qiE 'efi|eltorito|appended_partition'; then
  echo "FEHLER: Keine UEFI/El-Torito-Bootstruktur erkannt."
  exit 1
fi

echo
echo "== Sichtbarer /live-Inhalt =="
xorriso -indev "$ISO" -ls /live 2>/dev/null || \
  echo "WARNUNG: /live ist im sichtbaren ISO9660-Dateibaum nicht direkt auflistbar."

echo
echo "== Bootdateien diagnostisch suchen =="
ISO_LIST="$(xorriso -indev "$ISO" -find / -type f -print 2>/dev/null || true)"

KERNEL_MATCH="$(printf '%s\n' "$ISO_LIST" | grep -Ei '/(live|boot)/.*(vmlinuz|linux)' | head -n1 || true)"
INITRD_MATCH="$(printf '%s\n' "$ISO_LIST" | grep -Ei '/(live|boot)/.*(initrd|initramfs)' | head -n1 || true)"

if [[ -n "$KERNEL_MATCH" ]]; then
  echo "Kernel gefunden: $KERNEL_MATCH"
else
  echo "WARNUNG: Kernel nicht im sichtbaren ISO-Dateibaum gefunden."
  echo "         Der Dateiname oder die Bootstruktur kann je nach live-build-Version abweichen."
fi

if [[ -n "$INITRD_MATCH" ]]; then
  echo "Initrd gefunden: $INITRD_MATCH"
else
  echo "WARNUNG: Initrd nicht im sichtbaren ISO-Dateibaum gefunden."
fi

echo
echo "== Sichtbare GRUB-/EFI-Dateien =="
printf '%s\n' "$ISO_LIST" | grep -Ei 'grub\.cfg|config\.cfg|theme\.txt|grubx64|bootx64|efi' || true

echo
echo "== ISO Root =="
xorriso -indev "$ISO" -ls / 2>/dev/null || true

echo
echo "Boot-Struktur: El-Torito/UEFI erkannt; Dateipfade wurden diagnostisch ausgegeben."

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