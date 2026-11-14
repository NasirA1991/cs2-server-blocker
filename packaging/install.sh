#!/usr/bin/env bash
# Install cs2blocker system-wide.  Never enables or starts anything.
set -Eeuo pipefail
umask 022

ROOT=/
DAEMON_RELOAD=1
SKIP_DEPS=0
FORCE=0
UPGRADE=0
STAGE=
OLD_MANIFEST=
STEP=initialization
FAMILY=
OS_ID=
OS_LIKE=
declare -a PKGS=()

usage() {
  cat <<'USAGE'
Usage: sudo ./packaging/install.sh [OPTIONS]

Install cs2blocker under / (or DIR/usr with --root). This script never enables
or starts a service. It stages the complete tree, validates it, then commits it
to the target root.

Options:
  --root DIR             Install under DIR/usr instead of /.
  --no-daemon-reload     Do not run systemctl daemon-reload after installing.
  --skip-deps            Skip dependency checks (system installs only; intended
                         for controlled builds). The package-conflict guard
                         still applies; use --force for that.
  --force                Override the package-conflict guard. It never installs
                         on top of a package silently; use it only deliberately.
  -h, --help             Show this help.

The installer writes /usr/lib/cs2blocker/install-manifest.txt. On upgrade it
removes only files listed in the previous manifest that are absent from the new
one. Dependency checks apply only to a system install (--root is not checked).
USAGE
}

die() {
  printf 'install: %s\n' "$*" >&2
  exit 1
}

# User-facing refusal: no partial install exists, so suppress the ERR trap's
# "failed during" message and cleanup hint.
refuse() {
  printf 'install: %s\n' "$*" >&2
  trap - ERR
  exit 1
}

cleanup_stage() {
  local status=$?
  trap - EXIT
  if [[ -n ${STAGE:-} ]]; then
    rm -rf -- "$STAGE" || true
  fi
  if [[ -n ${OLD_MANIFEST:-} ]]; then
    rm -f -- "$OLD_MANIFEST" || true
  fi
  return "$status"
}

on_error() {
  local status=$?
  trap - ERR
  printf 'install: failed during "%s" (exit %s)\n' "$STEP" "$status" >&2
  if [[ $UPGRADE -eq 1 ]]; then
    printf 'Re-run the installer to complete the upgrade.\n' >&2
    printf 'If that is not possible, clean up the partial install with:\n' >&2
  else
    printf 'Clean up the partial install with:\n' >&2
  fi
  printf '  sudo ./packaging/uninstall.sh --yes --keep-config\n' >&2
  exit "$status"
}

trap cleanup_stage EXIT
trap on_error ERR

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      [[ $# -ge 2 ]] || refuse "--root needs a path"
      ROOT=$2
      shift 2
      ;;
    --no-daemon-reload)
      DAEMON_RELOAD=0
      shift
      ;;
    --skip-deps)
      SKIP_DEPS=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      refuse "unknown option: $1"
      ;;
  esac
done

ROOT=$(realpath -m -- "$ROOT")
if [[ $ROOT == "/" && ${EUID} -ne 0 ]]; then
  refuse "run as root for a system install: sudo $0"
fi

SRC=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ -d "$SRC/cs2blocker" ]] || refuse "source package is missing: $SRC/cs2blocker"
if [[ -L "$SRC/cs2blocker" ]] || [[ -n "$(find "$SRC/cs2blocker" -type l -print -quit)" ]]; then
  refuse "refusing to install: source package contains a symlink"
fi

have() {
  command -v -- "$1" >/dev/null 2>&1
}

os_release_value() {
  local key=$1 value
  value=$(sed -n "s/^${key}=//p" /etc/os-release 2>/dev/null | head -n 1)
  value=${value#\"}
  value=${value%\"}
  printf '%s\n' "$value"
}

detect_family() {
  OS_ID=$(os_release_value ID)
  OS_LIKE=$(os_release_value ID_LIKE)
  local combined="${OS_ID} ${OS_LIKE}"
  case "$combined" in
    *arch*) FAMILY=arch ;;
    *debian*|*ubuntu*) FAMILY=debian ;;
    *fedora*|*rhel*|*centos*) FAMILY=fedora ;;
    *suse*) FAMILY=suse ;;
    *) FAMILY=unknown ;;
  esac
}

add_package() {
  local package
  for package in "${PKGS[@]}"; do
    [[ $package == "$1" ]] && return 0
  done
  PKGS+=("$1")
}

add_python_packages() {
  case "$FAMILY" in
    arch) add_package python ;;
    *) add_package python3 ;;
  esac
}

add_gui_packages() {
  case "$FAMILY" in
    arch)
      add_package python-gobject
      add_package python-cairo
      add_package gtk4
      add_package libadwaita
      ;;
    debian)
      add_package python3-gi
      add_package python3-cairo
      add_package gir1.2-gtk-4.0
      add_package gir1.2-adw-1
      ;;
    fedora|suse)
      add_package python3-gobject
      add_package python3-cairo
      add_package gtk4
      add_package libadwaita
      ;;
    *)
      add_package python3-gobject
      add_package python3-cairo
      add_package gtk4
      add_package libadwaita
      ;;
  esac
}

add_polkit_packages() {
  case "$FAMILY" in
    debian)
      add_package polkitd
      add_package pkexec
      ;;
    *)
      add_package polkit
      ;;
  esac
}

print_install_command() {
  local package
  if ((${#PKGS[@]} == 0)); then
    return 0
  fi
  printf 'Install the missing packages with:\n' >&2
  case "$FAMILY" in
    arch)
      printf '  sudo pacman -S --needed' >&2
      for package in "${PKGS[@]}"; do
        printf ' %q' "$package" >&2
      done
      printf '\n' >&2
      ;;
    debian)
      printf '  sudo apt install' >&2
      for package in "${PKGS[@]}"; do
        printf ' %q' "$package" >&2
      done
      printf '\n' >&2
      ;;
    fedora)
      printf '  sudo dnf install' >&2
      for package in "${PKGS[@]}"; do
        printf ' %q' "$package" >&2
      done
      printf '\n' >&2
      ;;
    suse)
      printf '  sudo zypper install' >&2
      for package in "${PKGS[@]}"; do
        printf ' %q' "$package" >&2
      done
      printf '\n' >&2
      ;;
    *)
      printf '  Detected ID=%s ID_LIKE=%s; install these packages with your package manager: %s\n' \
        "${OS_ID:-unknown}" "${OS_LIKE:-unknown}" "${PKGS[*]}" >&2
      ;;
  esac
}

native_package_hint() {
  case "$FAMILY" in
    arch)
      printf 'Native package available: AUR cs2blocker; use an AUR helper for easier upgrades.\n'
      ;;
    debian)
      printf 'Native package available: .deb from the project Releases; use it for easier upgrades.\n'
      ;;
    fedora)
      printf 'Native package available: .rpm from the project Releases; use it for easier upgrades.\n'
      ;;
  esac
}

package_is_installed() {
  if have pacman && pacman -Qq cs2blocker >/dev/null 2>&1; then
    return 0
  fi
  if have dpkg && dpkg -s cs2blocker >/dev/null 2>&1; then
    return 0
  fi
  if have rpm && rpm -q cs2blocker >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

check_package_conflict() {
  if [[ $FORCE -eq 1 ]] || ! package_is_installed; then
    return 0
  fi
  cat >&2 <<'MSG'
install: cs2blocker is already installed by the system package manager.
Remove the package first, or upgrade with the package manager:
  Arch:   sudo pacman -Rns cs2blocker   (then reinstall the AUR package)
  Debian: sudo apt remove cs2blocker
  Fedora: sudo dnf remove cs2blocker
Use --force only if you are deliberately replacing the package with this
source install.
MSG
  trap - ERR
  exit 1
}

check_dependencies() {
  local required=0
  PKGS=()

  if ! have python3; then
    printf 'error: python3 is required but was not found.\n' >&2
    required=1
    add_python_packages
  elif ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    printf 'error: Python 3.11 or newer is required; python3 is %s.\n' \
      "$(python3 --version 2>&1)" >&2
    required=1
    add_python_packages
  fi

  if ! have nft; then
    if ! have iptables || ! have ipset; then
      printf 'error: neither nftables nor the iptables+ipset fallback is available.\n' >&2
      required=1
      add_package nftables
    fi
  fi

  if have python3 && ! python3 -c \
    "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); from gi.repository import Gtk, Adw; import cairo; assert Adw.MINOR_VERSION >= 5 or Adw.MAJOR_VERSION > 1" \
    >/dev/null 2>&1; then
    printf 'warning: the GUI stack is missing or too old. The CLI will work, but the GUI will not.\n' >&2
    if [[ $FAMILY == debian ]]; then
      printf 'warning: libadwaita 1.5+ requires Debian 13 or Ubuntu 24.04 or newer.\n' >&2
    fi
    add_gui_packages
  fi

  if ! have pkcheck && ! have pkexec; then
    printf 'warning: polkit/pkcheck was not found. Use "sudo cs2blockctl apply" or "cs2blockctl --sudo apply" for privileged actions.\n' >&2
    add_polkit_packages
  fi

  if ! have systemctl; then
    printf 'warning: systemctl was not found. See docs/NON_SYSTEMD.md for service and timer setup.\n' >&2
  fi

  if ((${#PKGS[@]})); then
    print_install_command
  fi
  if ((required)); then
    trap - ERR
    exit 1
  fi
}

if [[ $ROOT == "/" ]]; then
  detect_family
  check_package_conflict
  native_package_hint
  if [[ $SKIP_DEPS -eq 0 ]]; then
    check_dependencies
  fi
fi

STEP="prepare staging directory"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/cs2blocker-install.XXXXXX")
OLD_MANIFEST=$(mktemp "${TMPDIR:-/tmp}/cs2blocker-old-manifest.XXXXXX")
if [[ -f "$ROOT/usr/lib/cs2blocker/install-manifest.txt" ]]; then
  UPGRADE=1
  cp -- "$ROOT/usr/lib/cs2blocker/install-manifest.txt" "$OLD_MANIFEST"
fi

write_manifest() {
  local stage=$1
  local manifest="$stage/usr/lib/cs2blocker/install-manifest.txt"
  local temporary="$stage/install-manifest.tmp"
  find "$stage/usr" \( -type f -o -type l \) -printf '/usr/%P\n' \
    | LC_ALL=C sort -u > "$temporary"
  printf '%s\n' "/usr/lib/cs2blocker/install-manifest.txt" >> "$temporary"
  LC_ALL=C sort -u "$temporary" -o "$manifest"
  rm -f -- "$temporary"
  chmod 644 -- "$manifest"
}

install_stage() {
  local stage=$1
  local dest="$stage/usr"
  install -d "$dest/lib/cs2blocker" "$dest/bin" "$dest/lib/systemd/system" \
    "$stage/usr/share/polkit-1/actions" "$stage/usr/share/applications" \
    "$stage/usr/share/icons/hicolor/scalable/apps" "$stage/usr/share/man/man1" \
    "$stage/usr/share/metainfo" \
    "$stage/usr/share/bash-completion/completions" \
    "$stage/usr/share/zsh/site-functions" \
    "$stage/usr/share/fish/vendor_completions.d"

  # Never preserve a developer checkout's ownership: the helper imports this
  # code as root. Symlinks were rejected before staging.
  cp -a --no-preserve=ownership "$SRC/cs2blocker" "$dest/lib/cs2blocker/"
  find "$dest/lib/cs2blocker" -type d -name __pycache__ -prune -exec rm -rf {} +
  find "$dest/lib/cs2blocker" -type f -name '*.pyc' -delete
  chmod -R u+rwX,go+rX,go-w "$dest/lib/cs2blocker"
  if [[ ${EUID} -eq 0 ]]; then
    chown -R 0:0 "$dest/lib/cs2blocker"
  fi

  install -Dm755 "$SRC/packaging/launchers/cs2blocker-helper" "$dest/lib/cs2blocker/cs2blocker-helper"
  install -Dm755 "$SRC/packaging/launchers/cs2blockctl" "$dest/bin/cs2blockctl"
  install -Dm755 "$SRC/packaging/launchers/cs2blocker-gui" "$dest/bin/cs2blocker-gui"

  install -Dm644 "$SRC/packaging/systemd/cs2blocker.service" "$dest/lib/systemd/system/cs2blocker.service"
  install -Dm644 "$SRC/packaging/systemd/cs2blocker-refresh.service" "$dest/lib/systemd/system/cs2blocker-refresh.service"
  install -Dm644 "$SRC/packaging/systemd/cs2blocker-refresh.timer" "$dest/lib/systemd/system/cs2blocker-refresh.timer"
  install -Dm644 "$SRC/packaging/systemd/cs2blocker-monitor.service" "$dest/lib/systemd/system/cs2blocker-monitor.service"
  install -Dm644 "$SRC/packaging/polkit/org.cs2blocker.policy" "$stage/usr/share/polkit-1/actions/org.cs2blocker.policy"
  # Keep desktop file ID aligned with the Wayland app_id: org.cs2blocker.Gui
  install -Dm644 "$SRC/packaging/org.cs2blocker.Gui.desktop" "$stage/usr/share/applications/org.cs2blocker.Gui.desktop"
  install -Dm644 "$SRC/packaging/cs2blocker.svg" "$stage/usr/share/icons/hicolor/scalable/apps/cs2blocker.svg"
  install -Dm644 "$SRC/packaging/org.cs2blocker.Gui.metainfo.xml" "$stage/usr/share/metainfo/org.cs2blocker.Gui.metainfo.xml"
  install -Dm644 "$SRC/packaging/completions/cs2blockctl.bash" "$stage/usr/share/bash-completion/completions/cs2blockctl"
  install -Dm644 "$SRC/packaging/completions/_cs2blockctl" "$stage/usr/share/zsh/site-functions/_cs2blockctl"
  install -Dm644 "$SRC/packaging/completions/cs2blockctl.fish" "$stage/usr/share/fish/vendor_completions.d/cs2blockctl.fish"
  install -Dm644 "$SRC/packaging/man/cs2blockctl.1" "$stage/usr/share/man/man1/cs2blockctl.1"
  install -Dm644 "$SRC/LICENSE" "$stage/usr/share/licenses/cs2blocker/LICENSE"

  write_manifest "$stage"
}

cleanup_stale_files() {
  local old=$1
  local new_manifest="$ROOT/usr/lib/cs2blocker/install-manifest.txt"
  local path
  [[ -s $old ]] || return 0
  while IFS= read -r path; do
    [[ $path == /usr/* ]] || continue
    case "$path" in
      *'/../'*|*'/..') continue ;;
    esac
    if ! grep -Fqx -- "$path" "$new_manifest"; then
      rm -f -- "$ROOT$path"
    fi
  done < "$old"
}

check_root_parents() {
  local target=$1
  local manifest=$2
  local path parent_rel parent_dir powner pmode
  [[ -f $manifest ]] || return 0
  while IFS= read -r path; do
    [[ $path == /usr/* ]] || continue
    parent_rel=$(dirname -- "$path")
    while [[ $parent_rel == /usr || $parent_rel == /usr/* ]]; do
      parent_dir="$target$parent_rel"
      # Directories that do not exist yet will be created explicitly with 0755.
      if [[ -d $parent_dir ]]; then
        powner=$(stat -c '%u' -- "$parent_dir")
        pmode=$(stat -c '%a' -- "$parent_dir")
        if [[ $powner != 0 ]] || (( (8#$pmode & 8#022) != 0 )) || (( (8#$pmode & 8#001) != 8#001 )); then
          printf 'unsafe parent directory: %s (owner=%s mode=%s)\n' "$parent_rel" "$powner" "$pmode" >&2
          refuse "system install has an unsafe parent directory"
        fi
      fi
      [[ $parent_rel == /usr ]] && break
      parent_rel=$(dirname -- "$parent_rel")
    done
  done < "$manifest"
}

check_root_security() {
  local target=$1
  local code="$target/usr/lib/cs2blocker"
  local manifest="$code/install-manifest.txt"
  local bad path file owner mode
  [[ -f "$code/cs2blocker-helper" ]] || die "installed helper is missing"
  bad=$(find "$code" \( ! -user root -o -perm /022 \) -print)
  if [[ -n $bad ]]; then
    printf '%s\n' "$bad" >&2
    die "installed helper code is not root-owned or is group/world-writable"
  fi
  while IFS= read -r path; do
    [[ $path == /usr/* ]] || continue
    file="$target$path"
    [[ -e $file ]] || die "manifest entry disappeared during install: $path"
    owner=$(stat -c '%u' -- "$file")
    mode=$(stat -c '%a' -- "$file")
    if [[ $owner != 0 ]] || (( (8#$mode & 8#022) != 0 )); then
      printf 'unsafe installed file: %s (owner=%s mode=%s)\n' "$path" "$owner" "$mode" >&2
      die "system install contains a non-root-owned or writable file"
    fi
  done < "$manifest"
}

STEP="build staged tree"
install_stage "$STAGE"

STEP="validate staged ownership and permissions"
if [[ $ROOT == "/" ]]; then
  check_root_security "$STAGE"
fi

STEP="validate real install parent directories before writing"
if [[ $ROOT == "/" ]]; then
  check_root_parents "$ROOT" "$STAGE/usr/lib/cs2blocker/install-manifest.txt"
fi

STEP="create application directories"
install -d -m 0755 -- "$ROOT/usr/lib/cs2blocker" "$ROOT/usr/share/licenses/cs2blocker"
while IFS= read -r source_dir; do
  rel=${source_dir#"$STAGE"}
  install -d -m 0755 -- "$ROOT$rel"
done < <(find "$STAGE/usr/lib/cs2blocker" "$STAGE/usr/share/licenses/cs2blocker" -type d -print)

STEP="install files from staged manifest"
while IFS= read -r path; do
  [[ $path == /usr/* ]] || die "invalid manifest path: $path"
  source="$STAGE$path"
  destination="$ROOT$path"
  [[ -f $source ]] || die "manifest entry missing from staging: $path"
  file_mode=$(stat -c '%a' -- "$source")
  install -D -m "$file_mode" -- "$source" "$destination"
done < "$STAGE/usr/lib/cs2blocker/install-manifest.txt"

if [[ ${EUID} -eq 0 ]]; then
  chown -R 0:0 "$ROOT/usr/lib/cs2blocker"
  chmod -R u+rwX,go+rX,go-w "$ROOT/usr/lib/cs2blocker"
fi
chmod 644 -- "$ROOT/usr/lib/cs2blocker/install-manifest.txt"
# Remove the pre-org.cs2blocker.Gui desktop entry if it is still present.
rm -f -- "$ROOT/usr/share/applications/cs2blocker.desktop"

STEP="validate installed ownership and permissions"
if [[ $ROOT == "/" ]]; then
  check_root_security "$ROOT"
fi

STEP="remove files from the previous manifest"
cleanup_stale_files "$OLD_MANIFEST"

STEP="remove empty application directories"
if [[ -d "$ROOT/usr/lib/cs2blocker" ]]; then
  find "$ROOT/usr/lib/cs2blocker" -depth -type d -empty -delete
fi

STEP="refresh desktop and service caches"
if [[ $ROOT == "/" ]] && have gtk-update-icon-cache; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi
if [[ $DAEMON_RELOAD -eq 1 && $ROOT == "/" ]] && have systemctl; then
  systemctl daemon-reload || true
fi

cat <<MSG
Installed cs2blocker under $ROOT.
Nothing was enabled or started. Next steps:
  sudo systemctl enable --now cs2blocker.service
  cs2blocker-gui
  cs2blockctl list

Boot persistence can also be toggled from the GUI.
MSG
