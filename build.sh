#!/usr/bin/env bash
#
# build.sh — Build + install Cash Terminal (self-contained).
#
# Runs from a plain bash shell and does EVERYTHING itself:
#   1. Creates an isolated venv (with system gi/cairo) for the app.
#   2. Builds the Rust core (cashterm_core) into that venv via maturin.
#   3. Packages the Python front-end into a zipapp (.pyz).
#   4. Installs a tiny launcher to /usr/local/sbin/cash-terminal that
#      runs the zipapp under the venv python — no env activation needed.
#
# The parent shell is never modified: the venv is only ever activated
# inside scoped subshells, so you return to a normal bash prompt.
#
# Usage:
#   ./build.sh          — build + install
#   ./build.sh remove   — uninstall
#
set -euo pipefail

APP_NAME="cash-terminal"
INSTALL_DIR="/usr/local/sbin"
INSTALL_PATH="${INSTALL_DIR}/${APP_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="${SCRIPT_DIR}/cash_terminal"
CORE_DIR="${SCRIPT_DIR}/cashterm_core"

# Stable, user-owned runtime location for the venv + zipapp.
DATA_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/${APP_NAME}"
VENV_DIR="${DATA_DIR}/venv"
VENV_PY="${VENV_DIR}/bin/python3"
PYZ_PATH="${DATA_DIR}/${APP_NAME}.pyz"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ---- Uninstall ----
if [[ "${1:-}" == "remove" ]]; then
    info "Removing ${INSTALL_PATH} ..."
    sudo rm -f "${INSTALL_PATH}"
    info "Removing runtime data ${DATA_DIR} ..."
    rm -rf "${DATA_DIR}"
    # Clean up icons/desktop created by the app on first run.  Launchers are
    # matched by content, not by name, so entries left by an older identifier
    # go too — and a future rename needs no edit here.  The StartupWMClass
    # test mirrors _remove_stale_desktop_entries() in app.py: it is what the
    # app's own writer produces, so a launcher the user made by hand is left
    # alone rather than deleted out from under them.
    _apps_dir="${HOME}/.local/share/applications"
    if [[ -d "${_apps_dir}" ]]; then
        for _f in "${_apps_dir}"/*.desktop; do
            [[ -f "${_f}" ]] || continue
            _stem="$(basename "${_f}" .desktop)"
            if grep -qx 'Exec=cash-terminal' "${_f}" \
               && grep -qx "StartupWMClass=${_stem}" "${_f}"; then
                rm -f "${_f}"
            fi
        done
    fi
    rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/cash-terminal.svg"
    for _sz in 16 22 24 32 48 64 96 128 256; do
        rm -f "${HOME}/.local/share/icons/hicolor/${_sz}x${_sz}/apps/cash-terminal.png"
    done
    info "Done."
    exit 0
fi

# ---- Supported system ----
# Building is supported on Ubuntu 24.04 and on distributions based on it
# (Linux Mint 22, Pop!_OS 24.04, …) — that release only.  Older ones lack what
# the front-end needs (GTK 4.12+; 22.04 ships 4.6); newer ones are simply not
# supported yet (26.04 would also need a newer pyo3 for its Python).
UBUNTU_SUPPORTED_CODENAME="noble"
UBUNTU_SUPPORTED="24.04"

if [[ ${EUID} -eq 0 ]]; then
    error "Run ./build.sh as your normal user, not as root: it calls sudo itself where needed, and the app is installed into your home directory (${DATA_DIR})."
fi

# Read os-release in a subshell so its variables (ID, NAME, VERSION, …) do not
# leak into this script.  "|| true": under set -e a failing $(...) in an
# assignment would end the script silently, without the error below.
os_field() { ( . /etc/os-release 2>/dev/null && eval "printf '%s' \"\${$1:-}\"" ) || true; }
OS_NAME="$(os_field PRETTY_NAME)"
# One check covers Ubuntu itself and every derivative: all of them name their
# Ubuntu base in UBUNTU_CODENAME (a derivative's own VERSION_ID is its own
# numbering — Mint 22.1 — and says nothing about the base).  A system that is
# not Ubuntu-based has no such field and fails the same test.
if [[ "$(os_field UBUNTU_CODENAME)" != "${UBUNTU_SUPPORTED_CODENAME}" ]]; then
    error "Unsupported system: ${OS_NAME:-unknown}. Building is supported on Ubuntu ${UBUNTU_SUPPORTED} (${UBUNTU_SUPPORTED_CODENAME}) and distributions based on it only."
fi

command -v sudo >/dev/null 2>&1 || error "sudo not found. As root: apt-get install sudo"

# ---- Check dependencies ----
# Everything a fresh install lacks is collected first, then installed in one
# go — but only after asking.  Checks test what the build and the app actually
# use (an import, a command), so a dependency satisfied some other way is not
# reinstalled; each failed check names the apt package that provides it.
#
# Rust is the exception to apt: Ubuntu 24.04's cargo is 1.75, and the locked
# dependency tree needs RUST_MIN — set by home 0.5.12, a Linux dependency of
# alacritty_terminal 0.24.2, which declares rust-version = 1.88.  So it comes
# from rustup.  After updating Cargo.lock, re-check: the highest rust-version
# among the crates cargo compiles for Linux.
RUST_MIN="1.88"

APT_MISSING=()
RUST_ACTION=""          # "" | install (no rustup yet) | update (rustup, old toolchain)
RUST_FOUND=""

need_apt() {
    local p
    for p in "$@"; do
        [[ " ${APT_MISSING[*]-} " == *" ${p} "* ]] || APT_MISSING+=("${p}")
    done
}

pkg_installed() {
    # One status per installed architecture: a multi-arch package installed
    # for amd64 and i386 alike (librsvg2-common, on any system with i386
    # enabled) prints "installed" twice, so compare line by line, never the
    # whole output.  Captured first rather than piped into grep -q: grep quits
    # at the first match, dpkg-query gets SIGPIPE, and pipefail turns that
    # into a false "not installed".
    local status
    status="$(dpkg-query -W -f='${db:Status-Status}\n' "$1" 2>/dev/null || true)"
    grep -qx installed <<< "${status}"
}

# rustup installs cargo into ~/.cargo/bin, which is often not on a plain
# (non-login) bash PATH — pull it in so an existing toolchain is found.
load_cargo_env() {
    if [[ -f "${HOME}/.cargo/env" ]]; then
        # shellcheck disable=SC1091
        source "${HOME}/.cargo/env"
    elif [[ -d "${HOME}/.cargo/bin" && ":${PATH}:" != *":${HOME}/.cargo/bin:"* ]]; then
        export PATH="${HOME}/.cargo/bin:${PATH}"
    fi
}

check_deps() {
    APT_MISSING=()
    RUST_ACTION=""

    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import venv, ensurepip' 2>/dev/null || need_apt python3-venv
        python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' \
            2>/dev/null || need_apt python3-gi gir1.2-gtk-4.0
        python3 -c 'import gi, cairo; gi.require_foreign("cairo")' 2>/dev/null \
            || need_apt python3-gi-cairo
    else
        need_apt python3 python3-venv python3-gi gir1.2-gtk-4.0 python3-gi-cairo
    fi
    # The app renders its own icon from SVG through GdkPixbuf.
    pkg_installed librsvg2-common || need_apt librsvg2-common
    # Rust links the extension with the system C toolchain.
    command -v cc >/dev/null 2>&1 || need_apt build-essential

    load_cargo_env
    RUST_FOUND="$(rustc --version 2>/dev/null | awk '{print $2}' || true)"
    if [[ -z "${RUST_FOUND}" ]] || ! dpkg --compare-versions "${RUST_FOUND}" ge "${RUST_MIN}"; then
        if command -v rustup >/dev/null 2>&1; then
            RUST_ACTION="update"
        else
            RUST_ACTION="install"
            command -v curl >/dev/null 2>&1 || need_apt curl
            pkg_installed ca-certificates || need_apt ca-certificates
        fi
    fi
}

deps_missing() { (( ${#APT_MISSING[@]} )) || [[ -n "${RUST_ACTION}" ]]; }

describe_missing() {
    if (( ${#APT_MISSING[@]} )); then
        warn "Missing packages: ${APT_MISSING[*]}"
    fi
    case "${RUST_ACTION}" in
        install) warn "Rust ${RUST_MIN}+ is needed${RUST_FOUND:+ (found ${RUST_FOUND})}: rustup (https://rustup.rs) will install it into ~/.cargo." ;;
        update)  warn "Rust ${RUST_MIN}+ is needed${RUST_FOUND:+ (found ${RUST_FOUND})}: rustup will update the stable toolchain." ;;
    esac
    return 0
}

print_manual() {
    echo "Install them yourself, then run ./build.sh again:"
    if (( ${#APT_MISSING[@]} )); then
        echo "  sudo apt-get update && sudo apt-get install -y ${APT_MISSING[*]}"
    fi
    case "${RUST_ACTION}" in
        install) echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal" ;;
        update)  echo "  rustup update stable && rustup default stable" ;;
    esac
    return 0
}

# Asks on the terminal; with no terminal to ask on (a pipe, CI) the answer is
# no — nothing is ever installed unasked.  "н" is accepted too: it is the y key
# on a Russian keyboard layout.
ask_yes() {
    local reply=""
    [[ -t 0 ]] || return 1
    read -r -p "$1 [y/N] " reply || return 1
    case "${reply}" in
        y|Y|yes|Yes|YES|н|Н) return 0 ;;
        *) return 1 ;;
    esac
}

info "Checking dependencies (${OS_NAME})..."
check_deps
if deps_missing; then
    describe_missing
    if ! ask_yes "Install the missing dependencies now?"; then
        print_manual
        error "Missing dependencies — nothing was installed."
    fi
    if (( ${#APT_MISSING[@]} )); then
        info "Installing ${APT_MISSING[*]} ..."
        sudo apt-get update || error "apt-get update failed."
        sudo apt-get install -y "${APT_MISSING[@]}" || error "apt-get install failed."
    fi
    case "${RUST_ACTION}" in
        install)
            info "Installing Rust with rustup ..."
            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
                | sh -s -- -y --profile minimal || error "rustup installation failed."
            ;;
        update)
            info "Updating the Rust toolchain with rustup ..."
            { rustup update stable && rustup default stable; } || error "rustup update failed."
            ;;
    esac
    # Check again rather than trust the installers' exit codes: what matters
    # is that every check passes now.
    check_deps
    if deps_missing; then
        describe_missing
        error "Dependencies are still missing after the installation."
    fi
fi

[[ -d "${PKG_DIR}"  ]] || error "Package directory not found: ${PKG_DIR}"
[[ -d "${CORE_DIR}" ]] || error "Rust core directory not found: ${CORE_DIR}"
[[ -f "${CORE_DIR}/Cargo.toml" ]] || error "Cargo.toml not found in ${CORE_DIR}"

info "All dependencies OK (Rust ${RUST_FOUND})."

# ---- Create the isolated runtime venv ----
# --system-site-packages so the venv sees the distro's PyGObject / cairo,
# which cannot be pip-installed reliably.
if [[ ! -x "${VENV_PY}" ]]; then
    info "Creating venv at ${VENV_DIR} ..."
    mkdir -p "${DATA_DIR}"
    python3 -m venv --system-site-packages "${VENV_DIR}"
else
    info "Reusing existing venv at ${VENV_DIR}."
fi

# ---- Ensure build/runtime python deps inside the venv ----
info "Ensuring venv build tools (maturin, pyyaml) ..."
"${VENV_PY}" -m pip install --upgrade --quiet pip                  || warn "pip self-upgrade failed (continuing)."
"${VENV_PY}" -m pip install --quiet "maturin>=1.0,<2.0" pyyaml     || error "Failed to install maturin/pyyaml into the venv."

# ---- Build the Rust core into the venv ----
# Done in a scoped subshell: VIRTUAL_ENV + PATH only affect this subshell,
# so the parent shell stays a plain bash with no venv activated.
info "Building Rust core (maturin develop --release) ..."
(
    cd "${CORE_DIR}"
    VIRTUAL_ENV="${VENV_DIR}" \
    PATH="${VENV_DIR}/bin:${PATH}" \
        maturin develop --release
) || error "Rust core build failed."
info "Rust core built and installed into the venv."

# ---- Build zipapp (the Python front-end) ----
info "Building zipapp ..."

BUILD_TMP="$(mktemp -d)"
# Staged next to the final file, not in /tmp, so the install below is a
# same-filesystem rename.  /tmp is usually a separate mount (often tmpfs),
# where mv silently degrades to copy-then-unlink and loses atomicity.
mkdir -p "${DATA_DIR}"
PYZ_TMP="${PYZ_PATH}.new.$$"
trap 'rm -rf "${BUILD_TMP}" "${PYZ_TMP}"' EXIT

# Stage the package + a top-level __main__.py entry point.
cp -r "${PKG_DIR}" "${BUILD_TMP}/cash_terminal"
find "${BUILD_TMP}" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "${BUILD_TMP}/__main__.py" << 'EOF'
from cash_terminal.app import main

if __name__ == "__main__":
    main()
EOF

"${VENV_PY}" -m zipapp "${BUILD_TMP}" -o "${PYZ_TMP}" -p "/usr/bin/env python3"
# Install by rename, never by copying over the live file.  cp truncates the
# destination and refills it, so a rebuild while the terminal is running (or
# while another instance is starting) exposes a half-written archive.  rename()
# is atomic: readers see either the whole old file or the whole new one.
mv -f "${PYZ_TMP}" "${PYZ_PATH}"
info "zipapp built → ${PYZ_PATH}"

# ---- Install launcher wrapper ----
# Tiny shim that runs the zipapp under the venv python (which has the
# compiled cashterm_core extension).  A native .so cannot live inside a
# zipapp, so the venv is what makes the core importable at runtime.
info "Installing launcher ${INSTALL_PATH} ..."
WRAPPER_TMP="$(mktemp)"
cat > "${WRAPPER_TMP}" << EOF
#!/usr/bin/env bash
# Auto-generated launcher for Cash Terminal. Do not edit.
exec "${VENV_PY}" "${PYZ_PATH}" "\$@"
EOF
sudo cp "${WRAPPER_TMP}" "${INSTALL_PATH}"
sudo chmod +x "${INSTALL_PATH}"
rm -f "${WRAPPER_TMP}"

info "Installed successfully!"
info ""
info "Run with:  ${APP_NAME}"
info "Remove:    $0 remove"
