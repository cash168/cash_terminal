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

# ---- Check dependencies ----
info "Checking dependencies..."

command -v python3 >/dev/null 2>&1 || error "python3 not found. Install: sudo apt install python3"

python3 -c "import gi" 2>/dev/null || error "PyGObject not found. Install: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0"

python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
" 2>/dev/null || error "GTK4 GI bindings not found. Install: sudo apt install gir1.2-gtk-4.0"

# The terminal grid is rendered by the Rust core (cashterm_core), a compiled
# pyo3 extension built here with maturin.  Building it needs a Rust toolchain.
# rustup installs cargo into ~/.cargo/bin, which is often not on a plain
# (non-login) bash PATH — pull it in so the build works out of the box.
if ! command -v cargo >/dev/null 2>&1; then
    if [[ -f "${HOME}/.cargo/env" ]]; then
        # shellcheck disable=SC1091
        source "${HOME}/.cargo/env"
    elif [[ -x "${HOME}/.cargo/bin/cargo" ]]; then
        export PATH="${HOME}/.cargo/bin:${PATH}"
    fi
fi
command -v cargo >/dev/null 2>&1 || error "cargo (Rust toolchain) not found. Install: https://rustup.rs"

[[ -d "${PKG_DIR}"  ]] || error "Package directory not found: ${PKG_DIR}"
[[ -d "${CORE_DIR}" ]] || error "Rust core directory not found: ${CORE_DIR}"
[[ -f "${CORE_DIR}/Cargo.toml" ]] || error "Cargo.toml not found in ${CORE_DIR}"

info "All dependencies OK."

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
PYZ_TMP="$(mktemp -u).pyz"
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
mkdir -p "${DATA_DIR}"
cp "${PYZ_TMP}" "${PYZ_PATH}"
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
