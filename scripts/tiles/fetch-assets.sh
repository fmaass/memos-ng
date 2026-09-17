#!/usr/bin/env bash
# Fetch the map style's fonts and sprites into a tiles directory.
#
#   fetch-assets.sh <tiles-dir>
#
# Writes <tiles-dir>/assets/fonts/... and <tiles-dir>/assets/sprites/..., which is what the
# basemap asset route serves. Self-hosting them is the whole point: a style that pointed at
# someone else's glyph or sprite host would tell that host every map the household opens.
#
# PINNED BY COMMIT, NOT BY TAG. The upstream assets repository publishes no releases and carries
# no tags (GitHub API, 2026-09-17: both lists empty) — it is a GitHub Pages branch. A commit sha is
# the pin that repository actually offers, and it is a stronger one than a tag, which can be moved.
# The sha below is recorded into BUILD so a deployment can say exactly which glyphs it is serving.
#
# Bumping it: pick a newer commit from the repository, change ASSETS_COMMIT, re-run. The files are
# overwritten in place; they are not version-named, because the style references them by fixed
# path and no client caches a directory of them the way a PMTiles client does.

set -euo pipefail

ASSETS_REPO="protomaps/basemaps-assets"
ASSETS_COMMIT="028c18f713baecad011301ff7a69acc39bcc2ae7"  # main @ 2025-10-31

TILES_DIR="${1:-}"
[ -n "$TILES_DIR" ] || { echo "usage: fetch-assets.sh <tiles-dir>" >&2; exit 2; }

mkdir -p "$TILES_DIR"
TILES_DIR="$(cd "$TILES_DIR" && pwd)"
ASSETS_DIR="${TILES_DIR}/assets"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/fetch-assets.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

TARBALL="https://codeload.github.com/${ASSETS_REPO}/tar.gz/${ASSETS_COMMIT}"
echo "fetch-assets.sh: downloading ${ASSETS_REPO}@${ASSETS_COMMIT:0:8}"
curl -fsSL "$TARBALL" -o "${WORK}/assets.tar.gz"

# Only the two directories the style needs; the repository also carries scripts and a README.
tar -xzf "${WORK}/assets.tar.gz" -C "$WORK" --strip-components=1 \
    "basemaps-assets-${ASSETS_COMMIT}/fonts" "basemaps-assets-${ASSETS_COMMIT}/sprites"

# Staged then swapped per directory, so a failed download cannot leave the served tree half
# replaced: the style would render some glyph ranges and silently drop the rest.
mkdir -p "$ASSETS_DIR"
for part in fonts sprites; do
    [ -d "${WORK}/${part}" ] || { echo "fetch-assets.sh: ${part} missing from the archive" >&2; exit 1; }
    rm -rf "${ASSETS_DIR}/${part}.old"
    if [ -d "${ASSETS_DIR}/${part}" ]; then mv "${ASSETS_DIR}/${part}" "${ASSETS_DIR}/${part}.old"; fi
    mv "${WORK}/${part}" "${ASSETS_DIR}/${part}"
    rm -rf "${ASSETS_DIR}/${part}.old"
    echo "fetch-assets.sh: ${part} — $(find "${ASSETS_DIR}/${part}" -type f | wc -l) files, $(du -sh "${ASSETS_DIR}/${part}" | cut -f1)"
done

# Record the pin next to the archives. Any previous line for it is dropped first so re-running
# leaves one truthful value rather than a history.
BUILD_FILE="${TILES_DIR}/BUILD"
touch "$BUILD_FILE"
grep -v '^assets_' "$BUILD_FILE" > "${BUILD_FILE}.tmp" || true
{
    echo "assets_repo=${ASSETS_REPO}"
    echo "assets_commit=${ASSETS_COMMIT}"
    echo "assets_fetched_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "${BUILD_FILE}.tmp"
mv "${BUILD_FILE}.tmp" "$BUILD_FILE"

echo "fetch-assets.sh: done — ${ASSETS_DIR}"
