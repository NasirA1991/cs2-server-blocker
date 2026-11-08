#!/usr/bin/env bash
# Build release artifacts locally. Requires nfpm and makepkg.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
VERSION=$(sed -n 's/^pkgver=//p' packaging/PKGBUILD | head -1)
PKGREL=$(sed -n 's/^pkgrel=//p' packaging/PKGBUILD | head -1)
DIST="$ROOT/packaging/dist"
STAGING="$ROOT/packaging/staging"
NFPM=${NFPM:-nfpm}
mkdir -p "$DIST"
rm -rf "$STAGING"
rm -f "$DIST"/*.deb "$DIST"/*.rpm "$DIST"/*.tar.gz "$DIST"/*.pkg.tar.zst

"$ROOT/packaging/install.sh" --root "$STAGING" --no-daemon-reload >/dev/null

"$NFPM" package --config packaging/nfpm.yaml --packager deb \
    --target "$DIST/cs2blocker_${VERSION}-${PKGREL}_amd64.deb"
"$NFPM" package --config packaging/nfpm.yaml --packager rpm \
    --target "$DIST/cs2blocker-${VERSION}-${PKGREL}.x86_64.rpm"

(
    cd packaging
    makepkg -f --nodeps >/dev/null
)
cp "packaging/cs2blocker-${VERSION}-${PKGREL}-any.pkg.tar.zst" "$DIST/"

git archive --format=tar.gz --prefix="cs2blocker-${VERSION}/" \
    -o "$DIST/cs2blocker-${VERSION}.tar.gz" HEAD

(
    cd "$DIST"
    sha256sum *.deb *.rpm *.pkg.tar.zst *.tar.gz > SHA256SUMS
)
ls -lh "$DIST"
