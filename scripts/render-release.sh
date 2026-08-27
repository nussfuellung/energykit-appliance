#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="${ENERGYKIT_GHCR_OWNER:?ENERGYKIT_GHCR_OWNER fehlt}"
REPO="${ENERGYKIT_REPOSITORY_URL:?ENERGYKIT_REPOSITORY_URL fehlt}"
OWNER="${OWNER,,}"
IMAGE="ghcr.io/${OWNER}/energykit-{arch}"
for f in "$ROOT/energykit/config.yaml" "$ROOT/installer/preload/energykit/config.yaml" "$ROOT/installer/config/includes.chroot/opt/energykit-preload/energykit/config.yaml"; do
  sed -i "s#__IMAGE_BASE__#${IMAGE}#g; s#__REPOSITORY_URL__#${REPO}#g" "$f"
done
for f in "$ROOT/installer/preload/energykit/README.md" "$ROOT/installer/config/includes.chroot/opt/energykit-preload/energykit/README.md"; do
  [[ -f "$f" ]] && sed -i "s#__REPOSITORY_URL__#${REPO}#g" "$f" || true
done
printf 'Rendered image: %s\nRepository: %s\n' "$IMAGE" "$REPO"
