#!/usr/bin/env bash
# Publish the self-hosted basemap archives into a tiles directory.
#
#   extract.sh <tiles-dir> <regions.geojson> [--build YYYYMMDD] [--dev small|large]
#                                            [--user uid:gid] [--helper-image IMAGE]
#
# --user is the numeric uid:gid that PUBLISHES: every write into the tiles directory is made as
# that user, both the extraction and every rename, copy and delete around it. It defaults to the
# caller's own, which is what an ordinary local tiles directory wants and leaves this script
# behaving exactly as it always has. Publishing into the production tiles share needs its owner,
# --user 1027:100 (see THE CONTAINER USER below).
#
# --helper-image is the small image those renames run in when --user is given (default busybox:stable;
# it must already be present locally — the script refuses before writing anything if it is not).
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
# THE CONTAINER USER. The archives are written by the go-pmtiles container, not by this shell, and
# on the production tiles share those are two different questions. The share is an NFS-mounted
# Synology export whose ACL decides by numeric uid and MASKS the unix mode: the directory is mode
# 0777 owned 1000:1000, yet a container running as 1000:1000 is refused
# ("open /tiles/<name>.tmp: permission denied") while the share's owner 1027:100 — the uid the app
# and worker containers run as — is accepted.
#
# The extraction is only half of it. This script also creates, renames and deletes files in that
# directory around the container — the .tmp it renames into place, the regions sidecar, the pruned
# generations, BUILD — and those run in the calling shell. Against the production share the caller
# is refused there too, so an override that covered the container alone would move the failure one
# line down and no further: the extract would succeed and the rename that publishes it would not.
# fetch-assets.sh does write from the host shell and works, which is what made this easy to
# misread; it writes as the operator into a directory the operator may write, which the share and
# the container case are not.
#
# So when --user is given, NOTHING in the tiles directory is written by this shell. Every create,
# rename and delete goes through tiles_do(), a throwaway container running as that same user, and
# the shell only reads (listing, sizes) and decides. Without --user nothing changes: the writes are
# ordinary shell builtins, no helper container is started, and no helper image is needed.
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
# The go-pmtiles image is distroless — /go-pmtiles and nothing else, no shell and an EMPTY
# /usr/bin — so it cannot perform the renames, and they need a second image. It is only ever
# started when --user is given, and the script checks it is present locally BEFORE it writes
# anything rather than discovering a missing image (or a registry outage) halfway through a
# publish. Any image with a POSIX shell will do; override it with --helper-image.
HELPER_IMAGE="busybox:stable"
PLANET_MAXZOOM=7
DETAIL_MAXZOOM=15
DEV_MAXZOOM=12
# Fixed, non-residential bounding boxes: the city of Zürich and the canton around it. They exist so
# a developer can extract something small twice and watch the second generation take over.
DEV_SMALL_BBOX="8.4470,47.3200,8.6250,47.4350"
DEV_LARGE_BBOX="8.3570,47.1590,8.9850,47.6950"

usage() {
    sed -n '2,9p' "$0" >&2
    exit 2
}

TILES_DIR=""
REGIONS=""
BUILD=""
DEV=""
# Empty means "the caller's own", resolved after parsing. An exported PMTILES_USER is deliberately
# NOT honoured: which uid wrote a published archive is part of what the release log has to show, so
# it comes from the command line an operator can read back, not from an invisible environment.
PMTILES_USER=""
USER_GIVEN=0
VALID=0

while [ $# -gt 0 ]; do
    case "$1" in
        --build) BUILD="${2:-}"; shift 2 ;;
        --dev) DEV="${2:-}"; shift 2 ;;
        --user)
            [ $# -ge 2 ] || { echo "extract.sh: --user needs a value" >&2; exit 2; }
            PMTILES_USER="$2"; USER_GIVEN=1; shift 2 ;;
        --helper-image)
            [ $# -ge 2 ] || { echo "extract.sh: --helper-image needs a value" >&2; exit 2; }
            HELPER_IMAGE="$2"; shift 2 ;;
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

# Checked before anything starts: docker would take a user NAME and resolve it inside an image that
# has no user database, and the share's ACL matches the numeric id in any case — both fail late and
# opaquely, halfway through a multi-gigabyte extract. `--user ""` is a refusal, not a fallback: a
# variable that expanded to nothing is the mistake most worth catching.
if [ "$USER_GIVEN" = 1 ]; then
    case "$PMTILES_USER" in
        *[!0-9:]*|*:*:*|:*|*:|*[!:]*[!0-9:]*) VALID=0 ;;
        *:*) VALID=1 ;;
        *) VALID=0 ;;
    esac
    if [ "$VALID" != 1 ]; then
        echo "extract.sh: --user must be a numeric uid:gid (the production tiles share is 1027:100)" >&2
        exit 2
    fi
else
    PMTILES_USER="$(id -u):$(id -g)"
fi

# Checked here, before the first byte is written: a helper image that is not on this machine would
# otherwise surface as a failed rename between an extracted .tmp and a published name, which is the
# one moment this script has no good answer for. No pull is attempted — a publish is not the place
# to discover that a registry is down.
if [ "$USER_GIVEN" = 1 ] && ! docker image inspect "$HELPER_IMAGE" >/dev/null 2>&1; then
    echo "extract.sh: helper image '$HELPER_IMAGE' is not present locally." >&2
    echo "extract.sh: --user publishes through it; run 'docker pull $HELPER_IMAGE' first, or pass --helper-image with one you have." >&2
    exit 2
fi
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

if [ "$USER_GIVEN" = 1 ]; then
    # Creating it is itself a write, and on a share the caller may not write it would fail here or,
    # worse, no-op on an existing directory and hide the denial until the first real file.
    [ -d "$TILES_DIR" ] || { echo "extract.sh: no such tiles directory: $TILES_DIR" >&2; exit 2; }
else
    mkdir -p "$TILES_DIR"
fi
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
    # --user keeps the produced archives owned by the caller rather than by root, and on the
    # production share it is what makes the write possible at all (see THE CONTAINER USER). The
    # tiles directory is mounted at /tiles and the regions file's directory read-only at /work.
    local mounts=(-v "${TILES_DIR}:/tiles")
    if [ -z "$DEV" ]; then
        mounts+=(-v "$(cd "$(dirname "$REGIONS")" && pwd):/work:ro")
    fi
    docker run --rm --user "$PMTILES_USER" "${mounts[@]}" "$PMTILES_IMAGE" "$@"
}

# Every write into the tiles directory, and the ONLY place this script performs one. Without
# --user it is the calling shell, exactly as it has always been — same commands, same order, no
# container started and no helper image required. With --user it is a throwaway container running
# as the publishing user, so a share that only its owner may write is written only by its owner.
#
# The snippet is POSIX sh operating on /tiles (and /src for the sidecar copy, mounted read-only);
# the paths inside it are container paths in both modes, and the shell mode rewrites them back.
# Nothing here decides anything: the caller keeps the .tmp-verify-rename order, the refusal to
# rewrite a published name, and the prune rules. This only decides WHO writes.
tiles_do() {
    local snippet="$1" src_dir="${2:-}"
    if [ "$USER_GIVEN" = 1 ]; then
        local mounts=(-v "${TILES_DIR}:/tiles")
        [ -n "$src_dir" ] && mounts+=(-v "${src_dir}:/src:ro")
        docker run --rm -i --user "$PMTILES_USER" "${mounts[@]}" "$HELPER_IMAGE" sh -c "$snippet"
    else
        local host_snippet="${snippet//\/tiles/$TILES_DIR}"
        [ -n "$src_dir" ] && host_snippet="${host_snippet//\/src/$src_dir}"
        sh -c "$host_snippet"
    fi
}

# One archive, published atomically: write to a temporary name, verify the STRUCTURE of what was
# written, and only then rename it into place. A half-downloaded file never becomes a served one,
# and the rename is what makes the new generation visible to the manifest.
#
#   publish <name> <regions-file-or-empty> [tool flags...]
#
# WHY THE REGIONS FILE IS PUBLISHED HERE, AND FIRST. The manifest selects the newest detail archive
# and its coverage answer is read from the regions file under that archive's stem, so the two are
# one generation and the ARCHIVE is the half that must arrive last. Copying the regions file after
# the archive's rename leaves a window — and, if the run is interrupted or the copy fails, a
# permanent state — where the selected archive has no regions at all, which nothing later repairs
# because a published name is never rewritten. A regions file beside an archive nobody can see yet
# costs nothing; the reverse is unrecoverable.
publish() {
    local name="$1" regions_src="$2"; shift 2
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
    tiles_do "rm -f '/tiles/${name}.tmp'"
    echo "extract.sh: extracting ${name} from ${SOURCE}"
    pmtiles extract "$SOURCE" "/tiles/${name}.tmp" "$@"
    pmtiles verify "/tiles/${name}.tmp"
    if [ -n "$regions_src" ]; then
        local stem="${name%.pmtiles}.regions.geojson"
        tiles_do "cp '/src/$(basename "$regions_src")' '/tiles/${stem}.tmp' && mv '/tiles/${stem}.tmp' '/tiles/${stem}'" \
            "$(cd "$(dirname "$regions_src")" && pwd)"
        echo "extract.sh: published ${stem}"
    fi
    tiles_do "mv '/tiles/${name}.tmp' '/tiles/${name}'"
    echo "extract.sh: published ${name} ($(stat -c%s "${TILES_DIR}/${name}") bytes)"
}

# Drop generations older than the previous one. Newest two of each kind are kept: the one just
# published and the one it replaced (still being read by any client mid-session). An archive's
# regions file shares its stem and goes with it: the two are one generation, and a regions file
# outliving its archive would be read as describing ground nothing covers any more.
prune() {
    local kind="$1" keep=2
    # shellcheck disable=SC2012  # ls -t orders by mtime, which is the publication order here
    ls -1t "${TILES_DIR}/${kind}-"*.pmtiles 2>/dev/null | tail -n +$((keep + 1)) | while read -r old; do
        echo "extract.sh: removing superseded ${old##*/}"
        tiles_do "rm -f '/tiles/${old##*/}' '/tiles/$(basename "${old%.pmtiles}").regions.geojson'"
    done
}

# The geometry the detail archive is cut to travels with it: picking that archive has to imply the
# ground it covers, otherwise "is this pin on the detailed part of the map" can only be guessed
# from a regions file that may already describe a later extraction. A dev preset cuts to a bbox
# instead, so even a regions file named on its command line does not describe what was extracted
# and none is published — a developer's small extract is never the answer to that question.
SIDECAR_SRC=""
[ -n "$DEV" ] || SIDECAR_SRC="$REGIONS"

publish "$PLANET_NAME" "" --maxzoom="$PLANET_MAXZOOM"
publish "$DETAIL_NAME" "$SIDECAR_SRC" "${SELECTOR[@]}" --maxzoom="$DETAIL_ZOOM"

prune planet
prune detail

# BUILD is rewritten last, so it never describes an archive that is not there yet.
#
# The style assets have their own lifecycle: fetch-assets.sh records which revision of them is
# being served by appending `assets_*` lines here, and those files stay on disk across a
# re-extract. Rewriting this file wholesale would drop the record while the assets it describes
# are still being served, so the existing lines are carried over verbatim.
BUILD_BODY="$(mktemp)"
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
} > "$BUILD_BODY"
# Composed in a caller-owned temporary file and piped in, so the bytes are decided here and only
# the write crosses into the tiles directory.
tiles_do "cat > /tiles/BUILD.tmp && mv /tiles/BUILD.tmp /tiles/BUILD" < "$BUILD_BODY"
rm -f "$BUILD_BODY"

echo "extract.sh: done — ${TILES_DIR}/BUILD"
