#!/usr/bin/env bash
# get.sh - choose and install the latest cs2blocker release.
#
# This is intentionally a single, self-contained script so it can be reviewed
# before it is run. It never enables or starts a service. The caller is expected
# to be the normal desktop user; only individual package-manager commands use
# sudo. Test-only override: GET_SH_BASE_URL points at a local release directory.
set -euo pipefail
umask 022

REPO="NasirA1991/cs2-server-blocker"
APP="cs2blocker"
MIN_VERSION="0.2.0"
STATE_DIR="${XDG_STATE_HOME:-${HOME:-/tmp}/.local/state}/$APP"
STATE_FILE="$STATE_DIR/get-sh.state"

ASSUME_YES=0
DRY_RUN=0
UNINSTALL=0
METHOD=""
VERSION=""
BASE_URL="${GET_SH_BASE_URL:-}"
BASE_URL="${BASE_URL%/}"
TMP_DIR=""
TAG=""
DOWNLOAD_BASE=""
ASSET=""
AUR_HELPER=""
AUR_BUILD=0
AUR_MANUAL=0
OS_ID=""
OS_LIKE=""
OS_NAME=""
OS_VERSION=""
FAMILY=""
ARCH=""
DISTRO_LABEL=""

cleanup() {
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -rf -- "$TMP_DIR"
  fi
}

die() {
  printf 'get.sh: %s\n' "$*" >&2
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

usage() {
  cat <<'USAGE'
Usage: get.sh [OPTIONS]

Install the latest CS2 Server Blocker release for this system.

Options:
  --method <name>   force a method: aur, deb, rpm, source
  --version <vX.Y.Z> install a specific release (default: latest)
  --uninstall       remove the installation recorded by get.sh
  --yes             do not ask for confirmation (no TTY needed)
  --dry-run         print the plan and commands; run nothing
  -h, --help        show this help

--user is rejected: the application needs a root helper and polkit action.

Environment (testing only):
  GET_SH_BASE_URL   local release directory containing SHA256SUMS and assets;
                    requires --version so the download is reproducible.
USAGE
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --method)
        [[ $# -ge 2 ]] || die "--method needs a value"
        METHOD=$2
        shift 2
        ;;
      --version)
        [[ $# -ge 2 ]] || die "--version needs a value"
        VERSION=$2
        shift 2
        ;;
      --uninstall) UNINSTALL=1; shift ;;
      --yes) ASSUME_YES=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --user)
        die "--user is not supported: cs2blocker needs a root helper and polkit action. Use a system install."
        ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done
}

normalize_version() {
  local value=${1#v}
  [[ $value =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid release version: $1"
  printf '%s\n' "$value"
}

version_is_supported() {
  local actual=$1
  [[ $(printf '%s\n%s\n' "$MIN_VERSION" "$actual" | sort -V | head -n1) == "$MIN_VERSION" ]]
}

detect_os() {
  local line key value
  if [[ -r /etc/os-release ]]; then
    while IFS='=' read -r line; do
      [[ -n $line && $line != \#* ]] || continue
      key=${line%%=*}
      value=${line#*=}
      value=${value%\"}
      value=${value#\"}
      case "$key" in
        ID) OS_ID=$value ;;
        ID_LIKE) OS_LIKE=$value ;;
        NAME) OS_NAME=$value ;;
        VERSION_ID) OS_VERSION=$value ;;
      esac
    done < /etc/os-release
  fi

  ARCH=$(uname -m)
  [[ $ARCH == "x86_64" ]] || die "get.sh supports x86_64 only; use the source install for $ARCH."

  DISTRO_LABEL=${OS_NAME:-${OS_ID:-Linux}}
  [[ -n $OS_VERSION ]] && DISTRO_LABEL="$DISTRO_LABEL $OS_VERSION"

  case " $OS_ID $OS_LIKE " in
    *" arch "*) FAMILY="arch" ;;
    *" debian "*|*" ubuntu "*) FAMILY="debian" ;;
    *" fedora "*|*" rhel "*|*" centos "*) FAMILY="rpm" ;;
    *) FAMILY="unknown" ;;
  esac
}

choose_method() {
  if [[ -n $METHOD ]]; then
    case "$METHOD" in
      aur|deb|rpm|source) ;;
      *) die "unknown method: $METHOD (use aur, deb, rpm or source)" ;;
    esac
  else
    case "$FAMILY" in
      arch) METHOD="aur" ;;
      debian) METHOD="deb" ;;
      rpm) METHOD="rpm" ;;
      *) METHOD="source" ;;
    esac
  fi

  if [[ $METHOD == "aur" && $FAMILY != "arch" ]]; then
    die "--method aur is only available on Arch-based distributions"
  fi
  if [[ $METHOD == "deb" ]] && ! have apt; then
    die "--method deb requires apt; use --method source"
  fi
  if [[ $METHOD == "rpm" ]] && ! have dnf; then
    die "--method rpm requires dnf; use --method source"
  fi

  if [[ $METHOD == "aur" ]]; then
    if have paru; then
      AUR_HELPER="paru"
    elif have yay; then
      AUR_HELPER="yay"
    elif have git && have makepkg; then
      AUR_BUILD=1
    else
      AUR_MANUAL=1
    fi
  fi
}

check_root() {
  if [[ ${EUID} -eq 0 ]]; then
    die "Run as your normal user; get.sh uses sudo only for the steps that need it."
  fi
}

check_sudo() {
  [[ $DRY_RUN -eq 1 ]] && return 0
  have sudo || die "sudo is required for installation. Install sudo, or use the documented distro package command."
}

detect_installed() {
  local owner="" binary="/usr/bin/cs2blocker-gui"
  if have pacman && pacman -Qqo "$binary" >/dev/null 2>&1; then
    owner="pacman"
  elif have dpkg && dpkg -S "$binary" >/dev/null 2>&1; then
    owner="dpkg"
  elif have rpm && rpm -qf "$binary" >/dev/null 2>&1; then
    owner="rpm"
  fi

  if [[ -z $owner ]]; then
    if [[ -x $binary ]]; then
      printf 'cs2blocker is already installed without a package-manager owner.\n'
      printf 'Remove it with: sudo ./packaging/uninstall.sh --yes\n'
      exit 0
    fi
    return 0
  fi

  printf 'cs2blocker is already installed by %s. Not installing a second copy.\n' "$owner"
  if [[ $DRY_RUN -eq 1 ]]; then
    printf 'A real run would offer an upgrade; nothing was changed.\n'
    return 0
  fi
  if [[ $owner == "pacman" && $FAMILY == "arch" && -n $AUR_HELPER && $METHOD == "aur" ]]; then
    printf 'Will run:\n  %s -S --needed %s\n' "$AUR_HELPER" "$APP"
    if confirm "Upgrade it through $AUR_HELPER instead"; then
      check_sudo
      "$AUR_HELPER" -S --needed "$APP"
      write_state "aur" ""
      printf 'Upgraded %s with %s.\n' "$APP" "$AUR_HELPER"
      exit 0
    fi
  fi

  case "$owner" in
    pacman) printf 'Upgrade with: sudo pacman -S %s\n' "$APP" ;;
    dpkg) printf 'Upgrade with: sudo apt install --only-upgrade %s\n' "$APP" ;;
    rpm) printf 'Upgrade with: sudo dnf upgrade %s\n' "$APP" ;;
  esac
  exit 0
}

run_as_root() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  sudo %q' "$1"
    shift
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  sudo "$@"
}

fetch_file() {
  local url=$1 dest=$2
  if have curl; then
    curl --fail --silent --show-error --location --retry 2 --connect-timeout 15 -o "$dest" "$url"
  elif have wget; then
    wget -q -O "$dest" "$url"
  else
    die "curl or wget is required"
  fi
}

need_tools() {
  [[ $DRY_RUN -eq 1 ]] && return 0
  local missing=()
  have curl || have wget || missing+=("curl")
  have tar || missing+=("tar")
  have sha256sum || missing+=("sha256sum")
  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi
  printf 'Missing required tools: %s\n' "${missing[*]}" >&2
  case "$FAMILY" in
    debian) printf 'Install them with: sudo apt update && sudo apt install -y curl tar coreutils\n' >&2 ;;
    rpm) printf 'Install them with: sudo dnf install -y curl tar coreutils\n' >&2 ;;
    arch) printf 'Install them with: sudo pacman -S --needed curl tar coreutils\n' >&2 ;;
    *) printf 'Install curl (or wget), tar and coreutils, then rerun get.sh.\n' >&2 ;;
  esac
  exit 1
}

confirm() {
  local prompt=$1 answer=""
  [[ $ASSUME_YES -eq 1 ]] && return 0
  [[ $DRY_RUN -eq 1 ]] && return 0
  if [[ ! -r /dev/tty ]]; then
    die "no TTY; re-run with --yes"
  fi
  printf '%s? [y/N] ' "$prompt" > /dev/tty
  read -r answer < /dev/tty || answer=""
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) die "aborted" ;;
  esac
}

api_release() {
  local api=$1 json=$2
  if ! fetch_file "$api" "$json"; then
    if [[ -r $json ]] && grep -qi 'API rate limit exceeded' "$json"; then
      die "GitHub API rate limit exceeded; set --version vX.Y.Z or try again later"
    fi
    die "could not query the GitHub release API"
  fi
  if grep -qi 'API rate limit exceeded' "$json"; then
    die "GitHub API rate limit exceeded; set --version vX.Y.Z or try again later"
  fi
  local tag
  tag=$(grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' "$json" | sed -n '1s/.*"\([^"]*\)"$/\1/p')
  [[ -n $tag ]] || die "could not find tag_name in the GitHub API response"
  printf '%s\n' "$tag"
}

expected_asset() {
  local method=$1 version=$2
  case "$method" in
    deb) printf 'cs2blocker_%s-1_amd64.deb\n' "$version" ;;
    rpm) printf 'cs2blocker-%s-1.x86_64.rpm\n' "$version" ;;
    source) printf 'cs2blocker-%s.tar.gz\n' "$version" ;;
    *) return 1 ;;
  esac
}

resolve_release() {
  if [[ -n $BASE_URL && -z $VERSION ]]; then
    die "GET_SH_BASE_URL requires --version so the test download is reproducible"
  fi

  if [[ -n $VERSION ]]; then
    VERSION=$(normalize_version "$VERSION")
    version_is_supported "$VERSION" || die "Release v$VERSION is older than the installer requires; use --method aur or wait for the next release."
    TAG="v$VERSION"
    if [[ -z $BASE_URL ]]; then
      DOWNLOAD_BASE="https://github.com/$REPO/releases/download/$TAG"
    else
      DOWNLOAD_BASE=$BASE_URL
    fi
    if [[ $METHOD != "aur" ]]; then
      ASSET=$(expected_asset "$METHOD" "$VERSION")
    fi
    return 0
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    VERSION="<latest>"
    TAG="latest"
    DOWNLOAD_BASE="https://github.com/$REPO/releases/latest/download"
    if [[ $METHOD != "aur" ]]; then
      ASSET="cs2blocker-<version>.<asset>"
    fi
    return 0
  fi

  local json="$TMP_DIR/release.json" tag
  tag=$(api_release "https://api.github.com/repos/$REPO/releases/latest" "$json")
  VERSION=$(normalize_version "$tag")
  version_is_supported "$VERSION" || die "Release $tag is older than the installer requires; use --method aur or wait for the next release."
  TAG=$tag
  DOWNLOAD_BASE="https://github.com/$REPO/releases/download/$TAG"
  ASSET=$(expected_asset "$METHOD" "$VERSION")

  if [[ $METHOD != "aur" ]] && ! grep -q "\"name\"[[:space:]]*:[[:space:]]*\"$ASSET\"" "$json"; then
    die "release $TAG does not contain $ASSET"
  fi
}

method_label() {
  case "$METHOD" in
    aur)
      if [[ -n $AUR_HELPER ]]; then
        printf 'AUR (%s)' "$AUR_HELPER"
      elif [[ $AUR_BUILD -eq 1 ]]; then
        printf 'AUR (makepkg)'
      else
        printf 'AUR (manual steps)'
      fi
      ;;
    deb) printf '.deb package' ;;
    rpm) printf '.rpm package' ;;
    source) printf 'source tarball' ;;
  esac
}

print_plan() {
  printf 'Detected: %s, %s.\n' "$DISTRO_LABEL" "$ARCH"
  printf 'Method: %s.\n' "$(method_label)"
  if [[ $METHOD != "aur" ]]; then
    printf 'Release: %s\n' "${VERSION:-latest}"
    printf 'Asset: %s\n' "${ASSET:-pending}"
  fi
  printf 'Will run:\n'
  case "$METHOD" in
    aur)
      if [[ -n $AUR_HELPER ]]; then
        printf '  %s -S --needed %s\n' "$AUR_HELPER" "$APP"
      elif [[ $AUR_BUILD -eq 1 ]]; then
        printf '  git clone https://aur.archlinux.org/%s.git\n' "$APP"
        printf '  (cd %s && makepkg -si%s)\n' "$APP" "$([[ $ASSUME_YES -eq 1 ]] && printf ' --noconfirm')"
      else
        printf '  git clone https://aur.archlinux.org/%s.git\n' "$APP"
        printf '  (cd %s && makepkg -si)\n' "$APP"
      fi
      ;;
    deb)
      printf '  download and verify %s from release %s\n' "$ASSET" "${TAG:-latest}"
      printf '  sudo apt install -y ./%s\n' "$ASSET"
      ;;
    rpm)
      printf '  download and verify %s from release %s\n' "$ASSET" "${TAG:-latest}"
      printf '  sudo dnf install -y ./%s\n' "$ASSET"
      ;;
    source)
      printf '  download and verify %s from release %s\n' "$ASSET" "${TAG:-latest}"
      printf '  sudo ./install.sh (or sudo ./packaging/install.sh)\n'
      ;;
  esac
}

download_verified() {
  local asset=$1 version=$2 sumfile="$TMP_DIR/SHA256SUMS"
  local url file expected actual
  if [[ -z $DOWNLOAD_BASE ]]; then
    DOWNLOAD_BASE="https://github.com/$REPO/releases/download/$TAG"
  fi
  url="$DOWNLOAD_BASE/$asset"
  file="$TMP_DIR/$asset"

  fetch_file "$DOWNLOAD_BASE/SHA256SUMS" "$sumfile" || die "could not download SHA256SUMS"
  expected=$(awk -v name="$asset" '$2 == name || $2 == "*" name { print $1; exit }' "$sumfile")
  [[ -n $expected ]] || die "checksum for $asset is missing from SHA256SUMS"
  fetch_file "$url" "$file" || die "could not download $asset"
  actual=$(sha256sum "$file" | awk '{print $1}')
  [[ $actual == "$expected" ]] || die "checksum mismatch for $asset; refusing to extract or run it"

  if have cosign; then
    fetch_file "$DOWNLOAD_BASE/SHA256SUMS.sig" "$TMP_DIR/SHA256SUMS.sig" || die "cosign is installed but the release signature is missing"
    fetch_file "$DOWNLOAD_BASE/SHA256SUMS.pem" "$TMP_DIR/SHA256SUMS.pem" || die "cosign is installed but the release certificate is missing"
    cosign verify-blob \
      --certificate "$TMP_DIR/SHA256SUMS.pem" \
      --signature "$TMP_DIR/SHA256SUMS.sig" \
      --certificate-identity-regexp "https://github.com/$REPO/.github/workflows/release.yml@refs/tags/v.*" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com \
      "$sumfile" >/dev/null || die "cosign signature verification failed for SHA256SUMS"
    printf 'Verified SHA256SUMS with cosign.\n' >&2
  else
    printf 'Verified %s against SHA256SUMS (cosign not installed).\n' "$asset" >&2
  fi
  printf '%s\n' "$file"
}

extract_release() {
  local tarball=$1
  if tar -tzf "$tarball" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    die "release archive contains an unsafe path; refusing to extract"
  fi
  mkdir -p "$TMP_DIR/src"
  tar --no-same-owner -xzf "$tarball" -C "$TMP_DIR/src"
  local dir
  dir=$(find "$TMP_DIR/src" -mindepth 1 -maxdepth 1 -type d -name "$APP-*" -print -quit)
  [[ -n $dir && -d $dir ]] || die "release archive does not contain a $APP directory"
  printf '%s\n' "$dir"
}

install_from_source() {
  local version=$1 tarball dir installer
  tarball=$(download_verified "$(expected_asset source "$version")" "$version")
  dir=$(extract_release "$tarball")
  if [[ -x "$dir/install.sh" ]]; then
    installer="$dir/install.sh"
  elif [[ -x "$dir/packaging/install.sh" ]]; then
    installer="$dir/packaging/install.sh"
  else
    die "release archive does not contain install.sh"
  fi
  ( cd "$dir" && sudo "$installer" )
  write_state "source" "$version"
}

install_deb() {
  local version=$1 file
  file=$(download_verified "$(expected_asset deb "$version")" "$version")
  sudo apt install -y "$file"
  write_state "deb" "$version"
}

install_rpm() {
  local version=$1 file
  file=$(download_verified "$(expected_asset rpm "$version")" "$version")
  sudo dnf install -y "$file"
  write_state "rpm" "$version"
}

install_aur() {
  if [[ -n $VERSION ]]; then
    die "--version is not supported with --method aur; AUR always installs its current pkgver"
  fi
  if [[ -n $AUR_HELPER ]]; then
    if [[ $ASSUME_YES -eq 1 ]]; then
      "$AUR_HELPER" -S --needed --noconfirm "$APP"
    else
      "$AUR_HELPER" -S --needed "$APP"
    fi
    write_state "aur" ""
    return 0
  fi
  if [[ $AUR_BUILD -eq 1 ]]; then
    git clone "https://aur.archlinux.org/$APP.git" "$TMP_DIR/$APP"
    ( cd "$TMP_DIR/$APP" && makepkg -si --noconfirm )
    write_state "aur" ""
    return 0
  fi
  cat <<MANUAL
Install the AUR package manually:
  git clone https://aur.archlinux.org/$APP.git
  cd $APP
  makepkg -si
MANUAL
  exit 0
}

write_state() {
  local method=$1 version=$2
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  {
    printf 'method=%s\n' "$method"
    printf 'version=%s\n' "$version"
  } > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
}

read_state() {
  local key value
  STATE_METHOD=""
  STATE_VERSION=""
  [[ -r $STATE_FILE ]] || return 1
  while IFS='=' read -r key value; do
    case "$key" in
      method) STATE_METHOD=$value ;;
      version) STATE_VERSION=$value ;;
    esac
  done < "$STATE_FILE"
  [[ -n $STATE_METHOD ]]
}

print_uninstall_commands() {
  cat <<'COMMANDS'
No get.sh state file found. Use the command for the installation method:
  Arch/AUR:      sudo pacman -R cs2blocker
  Debian/Ubuntu: sudo apt remove cs2blocker
  Fedora/RHEL:   sudo dnf remove cs2blocker
  source:        download the matching release tarball, verify it, then run
                 sudo ./uninstall.sh --yes (or sudo ./packaging/uninstall.sh --yes)
COMMANDS
}

uninstall_source() {
  local version=$1 tarball dir uninstaller
  VERSION=$version
  TAG="v$VERSION"
  if [[ -z $BASE_URL ]]; then
    DOWNLOAD_BASE="https://github.com/$REPO/releases/download/$TAG"
  else
    DOWNLOAD_BASE=$BASE_URL
  fi
  tarball=$(download_verified "$(expected_asset source "$VERSION")" "$VERSION")
  dir=$(extract_release "$tarball")
  if [[ -x "$dir/uninstall.sh" ]]; then
    uninstaller="$dir/uninstall.sh"
  elif [[ -x "$dir/packaging/uninstall.sh" ]]; then
    uninstaller="$dir/packaging/uninstall.sh"
  else
    die "release archive does not contain uninstall.sh"
  fi
  ( cd "$dir" && sudo "$uninstaller" --yes )
}

do_uninstall() {
  if ! read_state; then
    print_uninstall_commands
    return 0
  fi
  case "$STATE_METHOD" in
    aur)
      printf 'Detected: %s, %s.\nMethod: AUR (installed by get.sh).\n' "$DISTRO_LABEL" "$ARCH"
      printf 'Will run:\n  sudo pacman -R --noconfirm %s\n' "$APP"
      if [[ $DRY_RUN -eq 0 ]]; then check_sudo; confirm "Remove cs2blocker"; sudo pacman -R --noconfirm "$APP"; rm -f -- "$STATE_FILE"; fi
      ;;
    deb)
      printf 'Detected: %s, %s.\nMethod: .deb package (installed by get.sh).\n' "$DISTRO_LABEL" "$ARCH"
      printf 'Will run:\n  sudo apt remove -y %s\n' "$APP"
      if [[ $DRY_RUN -eq 0 ]]; then check_sudo; confirm "Remove cs2blocker"; sudo apt remove -y "$APP"; rm -f -- "$STATE_FILE"; fi
      ;;
    rpm)
      printf 'Detected: %s, %s.\nMethod: .rpm package (installed by get.sh).\n' "$DISTRO_LABEL" "$ARCH"
      printf 'Will run:\n  sudo dnf remove -y %s\n' "$APP"
      if [[ $DRY_RUN -eq 0 ]]; then check_sudo; confirm "Remove cs2blocker"; sudo dnf remove -y "$APP"; rm -f -- "$STATE_FILE"; fi
      ;;
    source)
      [[ -n $STATE_VERSION ]] || die "state file has no version for the source installation"
      printf 'Detected: %s, %s.\nMethod: source tarball (installed by get.sh).\n' "$DISTRO_LABEL" "$ARCH"
      printf 'Will verify %s and run its uninstaller.\n' "cs2blocker-$STATE_VERSION.tar.gz"
      if [[ $DRY_RUN -eq 1 ]]; then
        printf '  download and verify cs2blocker-%s.tar.gz\n' "$STATE_VERSION"
        printf '  sudo ./uninstall.sh --yes (or sudo ./packaging/uninstall.sh --yes)\n'
        return 0
      fi
      check_sudo
      confirm "Remove cs2blocker"
      need_tools
      uninstall_source "$STATE_VERSION"
      rm -f -- "$STATE_FILE"
      ;;
    *)
      die "unknown method in state file: $STATE_METHOD"
      ;;
  esac
}

main() {
  parse_args "$@"
  check_root

  TMP_DIR=$(mktemp -d)
  trap cleanup EXIT

  detect_os
  choose_method

  if [[ $UNINSTALL -eq 1 ]]; then
    do_uninstall
    return 0
  fi

  if [[ -n $VERSION && $METHOD == "aur" ]]; then
    die "--version is not supported with --method aur; AUR always installs its current pkgver"
  fi

  detect_installed

  if [[ $METHOD == "aur" && $AUR_MANUAL -eq 1 ]]; then
    print_plan
    printf '\nNo AUR helper or makepkg was found. The commands above are the manual steps; rerun after installing them.\n'
    [[ $DRY_RUN -eq 1 ]] && return 0
    return 1
  fi

  resolve_release
  print_plan

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '\nDry run: no commands were executed.\n'
    return 0
  fi

  check_sudo
  confirm "Proceed"
  need_tools

  case "$METHOD" in
    aur) install_aur ;;
    deb) install_deb "$VERSION" ;;
    rpm) install_rpm "$VERSION" ;;
    source) install_from_source "$VERSION" ;;
  esac

  cat <<NEXT
Installed cs2blocker $VERSION.
Nothing was enabled or started. Next steps:
  sudo systemctl enable --now cs2blocker.service
  cs2blocker-gui
  cs2blockctl list

Boot persistence can also be toggled from the GUI. Firewall rules apply
host-wide, not only to CS2.
NEXT
}

main "$@"
