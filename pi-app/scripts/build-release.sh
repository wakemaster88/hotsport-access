#!/usr/bin/env bash
# Baut ein Release-ZIP, das in den Hub geladen werden kann.
# Aufruf: ./scripts/build-release.sh 2026.05.18-1
set -euo pipefail

VERSION=${1:?"Aufruf: $0 <version>"}
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR=${REPO_DIR}/dist
BUILD_DIR=${DIST_DIR}/build-${VERSION}

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/hotsport-access-${VERSION}"
TARGET=${BUILD_DIR}/hotsport-access-${VERSION}

rsync -a --exclude __pycache__ --exclude '*.pyc' \
    "${REPO_DIR}/app/"      "${TARGET}/app/"
rsync -a --exclude __pycache__ \
    "${REPO_DIR}/updater/"  "${TARGET}/updater/"

install -m 0644 "${REPO_DIR}/requirements.txt"          "${TARGET}/requirements.txt"
install -m 0644 "${REPO_DIR}/requirements-rfid.txt"     "${TARGET}/requirements-rfid.txt"
install -m 0644 "${REPO_DIR}/requirements-camera.txt"   "${TARGET}/requirements-camera.txt"
echo "${VERSION}" > "${TARGET}/VERSION"

mkdir -p "${DIST_DIR}"
ZIP=${DIST_DIR}/hotsport-access-${VERSION}.zip
rm -f "${ZIP}"
( cd "${BUILD_DIR}" && zip -qr "${ZIP}" "hotsport-access-${VERSION}" )

# sha256sum (Linux) bzw. shasum -a 256 (macOS)
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${ZIP}" | awk '{print $1}' > "${DIST_DIR}/${VERSION}.sha256"
else
    shasum -a 256 "${ZIP}" | awk '{print $1}' > "${DIST_DIR}/${VERSION}.sha256"
fi

echo "OK"
echo "  ZIP:    ${ZIP}"
echo "  SHA256: $(cat "${DIST_DIR}/${VERSION}.sha256")"
echo
echo "Hochladen z.B.:"
echo "  scp ${ZIP} hotsport@hub.local:/var/lib/hotsport-hub/releases/"
