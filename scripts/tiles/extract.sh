#!/usr/bin/env bash
# Publish the self-hosted basemap archives into a tiles directory.
#
#   extract.sh <tiles-dir> <regions.geojson> [--build YYYYMMDD] [--dev small|large]
#
# Produces two PMTiles archives cut from the Protomaps daily planet build:
#   planet-<build>.pmtiles          the whole world, z0-7  — always renders something
#   detail-<build>-<sha8>.pmtiles   the regions with pins, z0-15 — street detail where it matters
#
# WHY THE NAMES CARRY A VERSION. A PMTiles client caches an archive's header and directory per URL,
# and a directory entry that says "this tile is absent" is never re-fetched. Republishing under the
# same name would therefore pair a stale directory with new bytes and leave permanent holes in the
# map for every client that already looked. So each publication gets a name that has never been
# used: <sha8> is the first 8 hex of the sha256 over the EFFECTIVE extraction spec — build date,
# maxzoom, and the exact geometry bytes that were selected (the regions file, or the dev preset's
# bbox). A new regions file, a different maxzoom, a different dev preset: each lands beside the
# others under its own name, and the manifest route points at whichever was published last.
#
# WHY THE PREVIOUS GENERATION STAYS. A client that is mid-session holds the previous archive's
# directory and will keep reading that file; deleting it in the same run that replaces it would
# break exactly those readers. Only generations OLDER than the previous one are removed.
#
# TOOL FLAGS — read off `go-pmtiles v1.31.2` itself (2026-09-17, image digest
# sha256:06574f01f55a78f78f887bc7ebf729a5c093c0d6e17d9876300cfcb0758b59d3), not assumed:
#   extract <input> <output>   --region=STRING  local GeoJSON Polygon or MultiPolygon file
#                              --bbox=STRING    min_lon,min_lat,max_lon,max_lat
#                              --minzoom=-1 --maxzoom=-1        inclusive; -1 means "as in source"
#                              --download-threads=4 --overfetch=0.05 --dry-run
#   verify <input>             verifies the archive STRUCTURE (not individual tile contents)
#   show <path>                --metadata --header-json --tilejson --public-url=STRING
# The input may be an https:// URL: extract issues range requests and downloads only what the
# selected area needs, so no copy of the ~138 GB planet build is ever stored locally.
#
# The build URL pattern is https://build.protomaps.com/<YYYYMMDD>.pmtiles (the directory listing
# at the site root answers 404; the dated objects answer 200).

set -euo pipefail

PMTILES_IMAGE="protomaps/go-pmtiles:v1.31.2"
PLANET_MAXZOOM=7
DETAIL_MAXZOOM=15
DEV_MAXZOOM=12
# Fixed, non-residential bounding boxes: the city of Zürich and the canton around it. They exist so
# a developer can extract something small twice and watch the second generation take over.
DEV_SMALL_BBOX="8.4470,47.3200,8.6250,47.4350"
DEV_LARGE_BBOX="8.3570,47.1590,8.9850,47.6950"

usage() {
    sed -n '2,6p' "$0" >&2
    exit 2
}

TILES_DIR=""
REGIONS=""
BUILD=""
DEV=""

while [ $# -gt 0 ]; do
    case "$1" in
        --build) BUILD="${2:-}"; shift 2 ;;
        --dev) DEV="${2:-}"; shift 2 ;;
        -h|--help) usage ;;
        -*) echo "extract.sh: unknown option $1" >&2; usage ;;
        *)
            if [ -z "$TILES_DIR" ]; then TILES_DIR="$1"
            elif [ -z "$REGIONS" ]; then REGIONS="$1"
            else echo "extract.sh: unexpected argument $1" >&2; usage
            fi
            shift ;;
    esac
done

[ -n "$TILES_DIR" ] || usage
case "$DEV" in
    "")
        [ -n "$REGIONS" ] || usage
        [ -f "$REGIONS" ] || { echo "extract.sh: no such regions file: $REGIONS" >&2; exit 2; }
        ;;
    small|large) ;;
    *) echo "extract.sh: --dev takes 'small' or 'large'" >&2; usage ;;
esac

# Default to yesterday's planet build: today's is published during the day and may not exist yet.
[ -n "$BUILD" ] || BUILD="$(date -u -d 'yesterday' +%Y%m%d)"
case "$BUILD" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    *) echo "extract.sh: --build must be YYYYMMDD, got '$BUILD'" >&2; exit 2 ;;
esac

mkdir -p "$TILES_DIR"
# Every path this script writes is built from this one, so nothing can land outside it.
TILES_DIR="$(cd "$TILES_DIR" && pwd)"
SOURCE="https://build.protomaps.com/${BUILD}.pmtiles"

if [ -n "$DEV" ]; then
    if [ "$DEV" = small ]; then BBOX="$DEV_SMALL_BBOX"; else BBOX="$DEV_LARGE_BBOX"; fi
    DETAIL_ZOOM="$DEV_MAXZOOM"
    SELECTOR=(--bbox="$BBOX")
    # The spec hash covers what was actually selected, so the two dev presets - and a maxzoom
    # change under either - can never collide on one name.
    SPEC="dev=${DEV} bbox=${BBOX} maxzoom=${DETAIL_ZOOM} build=${BUILD}"
    REGIONS_SHA="(dev preset ${DEV})"
else
    DETAIL_ZOOM="$DETAIL_MAXZOOM"
    SELECTOR=(--region="/work/$(basename "$REGIONS")")
    REGIONS_SHA="$(sha256sum "$REGIONS" | cut -d' ' -f1)"
    SPEC="regions=${REGIONS_SHA} maxzoom=${DETAIL_ZOOM} build=${BUILD}"
fi
SHA8="$(printf '%s' "$SPEC" | sha256sum | cut -c1-8)"

PLANET_NAME="planet-${BUILD}.pmtiles"
DETAIL_NAME="detail-${BUILD}-${SHA8}.pmtiles"

pmtiles() {
    # --user keeps the produced archives owned by the caller rather than by root. The tiles
    # directory is mounted at /tiles and the regions file's directory read-only at /work.
    local mounts=(-v "${TILES_DIR}:/tiles")
    if [ -z "$DEV" ]; then
        mounts+=(-v "$(cd "$(dirname "$REGIONS")" && pwd):/work:ro")
    fi
    docker run --rm --user "$(id -u):$(id -g)" "${mounts[@]}" "$PMTILES_IMAGE" "$@"
}

# One archive, published atomically: write to a temporary name, verify the STRUCTURE of what was
# written, and only then rename it into place. A half-downloaded file never becomes a served one,
# and the rename is what makes the new generation visible to the manifest.
publish() {
    local name="$1"; shift
    if [ -f "${TILES_DIR}/${name}" ]; then
        if pmtiles verify "/tiles/${name}"; then
            echo "extract.sh: ${name} is already published and verifies — skipping"
            return 0
        fi
        # A published name is never rewritten, not even to repair it, and never deleted either.
        # Clients cache an archive's directory against its URL and a file response reads its size
        # and timestamp separately from its bytes, so replacing the contents under a name that is
        # already in circulation hands somebody new bytes with an old map of where things are in
        # them — and deleting it takes the map away from clients mid-session. The way out is
        # forward: extract again under a build date nobody has seen, which produces a new name,
        # and let the manifest move to it.
        echo "extract.sh: ${TILES_DIR}/${name} exists but does not verify." >&2
        echo "extract.sh: a published name is never rewritten or deleted." >&2
        echo "extract.sh: re-run with a fresh build name (--build <newer YYYYMMDD>) — the repaired archive is published beside this one, and the manifest moves to it." >&2
        exit 2
    fi
    rm -f "${TILES_DIR}/${name}.tmp"
    echo "extract.sh: extracting ${name} from ${SOURCE}"
    pmtiles extract "$SOURCE" "/tiles/${name}.tmp" "$@"
    pmtiles verify "/tiles/${name}.tmp"
    mv "${TILES_DIR}/${name}.tmp" "${TILES_DIR}/${name}"
    echo "extract.sh: published ${name} ($(stat -c%s "${TILES_DIR}/${name}") bytes)"
}

# Drop generations older than the previous one. Newest two of each kind are kept: the one just
# published and the one it replaced (still being read by any client mid-session).
prune() {
    local kind="$1" keep=2
    # shellcheck disable=SC2012  # ls -t orders by mtime, which is the publication order here
    ls -1t "${TILES_DIR}/${kind}-"*.pmtiles 2>/dev/null | tail -n +$((keep + 1)) | while read -r old; do
        echo "extract.sh: removing superseded ${old##*/}"
        rm -f "$old"
    done
}

publish "$PLANET_NAME" --maxzoom="$PLANET_MAXZOOM"
publish "$DETAIL_NAME" "${SELECTOR[@]}" --maxzoom="$DETAIL_ZOOM"
prune planet
prune detail

# BUILD is rewritten last, so it never describes an archive that is not there yet.
#
# The style assets have their own lifecycle: fetch-assets.sh records which revision of them is
# being served by appending `assets_*` lines here, and those files stay on disk across a
# re-extract. Rewriting this file wholesale would drop the record while the assets it describes
# are still being served, so the existing lines are carried over verbatim.
ASSETS_LINES=""
if [ -f "${TILES_DIR}/BUILD" ]; then
    ASSETS_LINES="$(grep '^assets_' "${TILES_DIR}/BUILD" || true)"
fi
{
    echo "build=${BUILD}"
    echo "source=${SOURCE}"
    echo "pmtiles_image=${PMTILES_IMAGE}"
    echo "regions_sha256=${REGIONS_SHA}"
    echo "spec=${SPEC}"
    echo "spec_sha8=${SHA8}"
    echo "planet=${PLANET_NAME}"
    echo "planet_size=$(stat -c%s "${TILES_DIR}/${PLANET_NAME}")"
    echo "planet_maxzoom=${PLANET_MAXZOOM}"
    echo "detail=${DETAIL_NAME}"
    echo "detail_size=$(stat -c%s "${TILES_DIR}/${DETAIL_NAME}")"
    echo "detail_maxzoom=${DETAIL_ZOOM}"
    echo "extracted_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    [ -n "$ASSETS_LINES" ] && echo "$ASSETS_LINES"
} > "${TILES_DIR}/BUILD.tmp"
mv "${TILES_DIR}/BUILD.tmp" "${TILES_DIR}/BUILD"

echo "extract.sh: done — ${TILES_DIR}/BUILD"
