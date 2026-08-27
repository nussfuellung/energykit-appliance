#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
command -v lb >/dev/null || { echo "live-build fehlt. Debian/Ubuntu: sudo apt install live-build"; exit 1; }
rm -rf .build-cache binary* chroot* cache local .build
./auto/config
lb build
ISO="$(find . -maxdepth 1 -name 'live-image-amd64.hybrid.iso' -print -quit)"
if [[ -n "$ISO" ]]; then
  mv "$ISO" "../energykit-installer-amd64.iso"
  sha256sum ../energykit-installer-amd64.iso > ../energykit-installer-amd64.iso.sha256
  echo "Fertig: ../energykit-installer-amd64.iso"
fi
