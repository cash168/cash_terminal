#!/usr/bin/env python3
"""Diagnostic — report whether everything Cash Terminal needs is present.

Not part of the build; ./build.sh does its own checks and fails loudly.  This
script exists for the other case: the app misbehaves and you want to see, in
one screen, which piece is missing and what to install.

Run it two ways, because they answer different questions:

    python3 check_deps.py
        The system interpreter — checks the distro packages (PyGObject, GTK4,
        cairo) and the Rust toolchain.  cashterm_core is expected to come back
        absent here; it is never installed system-wide.

    ~/.local/share/cash-terminal/venv/bin/python3 check_deps.py
        The interpreter the launcher actually uses.  This is the one that must
        come out fully green, cashterm_core included.
"""
import os
import sys
import shutil
import subprocess

# Required GI namespaces, with the version the app pins via require_version.
# Getting the version wrong is not a soft failure: gi raises if a *different*
# version of the namespace was already loaded, so a mismatch here would break
# the app at import time.
GI_NAMESPACES = [
    ("Gtk", "4.0", True),
    ("Gdk", "4.0", True),
    ("GdkPixbuf", "2.0", True),
    # X11-only, used to set WM_CLASS / _NET_WM_ICON that GTK4 no longer sets
    # itself.  Absent under a pure Wayland session, where the app skips that
    # code path — so its absence is not an error.
    ("GdkX11", "4.0", False),
]

# Namespaces with no explicit version requirement (they come with PyGObject).
GI_PLAIN = ["Gio", "GLib", "GObject", "Pango", "PangoCairo"]

APT_HINTS = {
    "gi": "sudo apt install python3-gi python3-gi-cairo",
    "Gtk": "sudo apt install gir1.2-gtk-4.0",
    "Gdk": "sudo apt install gir1.2-gtk-4.0",
    "GdkPixbuf": "sudo apt install gir1.2-gdkpixbuf-2.0",
    "GdkX11": "sudo apt install gir1.2-gtk-4.0  (X11 sessions only)",
    "cairo": "sudo apt install python3-gi-cairo python3-cairo",
    "yaml": "sudo apt install python3-yaml   (or: pip install pyyaml)",
    "cargo": "https://rustup.rs",
}

_failures = []


def report(name, ok, detail="", required=True):
    """Print one aligned result line and remember hard failures."""
    if ok:
        status = "OK"
    elif required:
        status = "FAIL"
        _failures.append(name)
    else:
        status = "absent (optional)"
    line = f"  {name:<22} {status}"
    if detail:
        line += f"  — {detail}"
    print(line)


def section(title):
    print(f"\n{title}")
    print("  " + "-" * (len(title) + 2))


def hint_for(name):
    return APT_HINTS.get(name, "")


# ---------------------------------------------------------------------------
section("Interpreter")
print(f"  python                 {sys.version.split()[0]}  ({sys.executable})")

# The launcher runs the zipapp under a venv built by ./build.sh.  Knowing
# whether *this* interpreter is that venv decides how to read the
# cashterm_core result below, so work it out up front.
_data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
    os.path.expanduser("~"), ".local", "share")
VENV_DIR = os.path.join(_data_home, "cash-terminal", "venv")
VENV_PY = os.path.join(VENV_DIR, "bin", "python3")
# Compare sys.prefix, not sys.executable: venv/bin/python3 is a symlink to the
# system interpreter, so realpath() collapses the two and every run would
# claim to be the venv.
in_app_venv = os.path.realpath(sys.prefix) == os.path.realpath(VENV_DIR)
print(f"  app venv               {'this interpreter' if in_app_venv else VENV_PY}")

# ---------------------------------------------------------------------------
section("GTK4 stack (runtime)")
try:
    import gi
    report("PyGObject (gi)", True, getattr(gi, "__version__", ""))
except ImportError as e:
    report("PyGObject (gi)", False, f"{e}; {hint_for('gi')}")
    gi = None

if gi is not None:
    for ns, ver, required in GI_NAMESPACES:
        try:
            gi.require_version(ns, ver)
            __import__("gi.repository", fromlist=[ns])
            report(f"{ns} {ver}", True)
        except Exception as e:
            detail = str(e)
            if required and hint_for(ns):
                detail += f"; {hint_for(ns)}"
            report(f"{ns} {ver}", False, detail, required=required)

    for ns in GI_PLAIN:
        try:
            __import__("gi.repository", fromlist=[ns])
            report(ns, True)
        except Exception as e:
            report(ns, False, str(e))

try:
    import cairo
    report("pycairo", True, getattr(cairo, "version", ""))
except ImportError as e:
    report("pycairo", False, f"{e}; {hint_for('cairo')}")

try:
    import yaml
    report("PyYAML", True, getattr(yaml, "__version__", ""))
except ImportError as e:
    report("PyYAML", False, f"{e}; {hint_for('yaml')}")

# ---------------------------------------------------------------------------
section("Rust core")
try:
    import cashterm_core
    core_file = getattr(cashterm_core, "__file__", None)
    if core_file is None or not hasattr(cashterm_core, "PtyTerm"):
        # Run from the repo root, the *source* directory cashterm_core/ is
        # picked up as an implicit namespace package: the import succeeds, the
        # module is empty, and PtyTerm is nowhere.  Worth calling out rather
        # than reporting OK — anything importing it from here gets the shell of
        # a module and fails much later with a confusing error.
        report("cashterm_core", False,
               "shadowed by the source directory ./cashterm_core (namespace "
               "package, no compiled extension) — run this from outside the "
               "repo, or with the venv python",
               required=in_app_venv)
    else:
        report("cashterm_core", True, core_file)
except ImportError as e:
    if in_app_venv:
        report("cashterm_core", False, f"{e}; run ./build.sh")
    else:
        # Expected: the extension is installed into the app venv only, so a
        # system interpreter cannot see it.  Flagging this as a failure would
        # send you chasing a problem that does not exist.
        report("cashterm_core", False,
               "not visible to this interpreter — expected outside the app "
               "venv; re-run with the venv python (see the header)",
               required=False)

# ---------------------------------------------------------------------------
section("Build toolchain (only needed to run ./build.sh)")
cargo = shutil.which("cargo")
if not cargo:
    # rustup drops cargo in ~/.cargo/bin, which a non-login shell often does
    # not have on PATH — build.sh sources it explicitly, so look there too.
    _fallback = os.path.expanduser("~/.cargo/bin/cargo")
    if os.access(_fallback, os.X_OK):
        cargo = _fallback + "  (not on PATH; build.sh sources ~/.cargo/env)"

if cargo:
    version = ""
    try:
        version = subprocess.run(
            [cargo.split()[0], "--version"], capture_output=True, text=True,
            timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    report("cargo", True, version or cargo)
else:
    report("cargo", False, f"not found; {hint_for('cargo')}")

try:
    import maturin  # noqa: F401
    report("maturin", True)
except ImportError:
    # build.sh pip-installs maturin into the venv itself, so its absence from
    # the interpreter you happen to be running is not a problem.
    report("maturin", False,
           "installed into the venv by ./build.sh when missing",
           required=False)

# ---------------------------------------------------------------------------
section("Launcher")
launcher = shutil.which("cash-terminal") or "/usr/local/sbin/cash-terminal"
report("cash-terminal", os.access(launcher, os.X_OK),
       launcher if os.access(launcher, os.X_OK)
       else f"{launcher} not installed; run ./build.sh",
       required=False)

# ---------------------------------------------------------------------------
print()
if _failures:
    print(f"{len(_failures)} required item(s) missing: {', '.join(_failures)}")
    sys.exit(1)
print("All required dependencies present.")
