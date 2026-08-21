"""Configuration constants + linux.yaml loader.

Scalars here are rebound by _load_yaml_config(); always read them as
config.NAME at runtime, never `from .config import NAME`.
"""
import os
import yaml
from gi.repository import Gdk


# Path of the config file last loaded (set by _load_yaml_config). Saves target
# this file so the Settings dialog round-trips to whatever the user is running.
DEFAULT_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".cash-terminal", "linux.yaml")
LOADED_CONFIG_PATH = None

FONT_FAMILY = "Monospace"
FONT_SIZE_PT = 10.5
SCROLLBACK_LINES = 10000
BG_ALPHA = 0.85

# ANSI 16-color palette (Konsole defaults) — will be overridden by config
PALETTE_HEX = [
    # Normal 0-7
    "#000000", "#B21818", "#18B218", "#B26818",
    "#1818B2", "#B218B2", "#18B2B2", "#B2B2B2",
    # Bright 8-15
    "#686868", "#FF5454", "#54FF54", "#FFFF54",
    "#5454FF", "#FF54FF", "#54FFFF", "#FFFFFF",
]

FG_COLOR = "#B2B2B2"
BG_COLOR = "#000000"
CURSOR_FG = "#000000"
CURSOR_BG = "#B2B2B2"

# Tab switch animation duration in milliseconds.
# Reduced by 30% from the original 180ms for snappier switching.
TAB_SWITCH_ANIMATION_MS = 126  # 180ms * 0.7

# Tab bar theme (CSS hex)
TAB_THEME = {
    "inactive_gradient_top":    "#CACACA",
    "inactive_gradient_bottom": "#F1F1F1",
    "active_gradient_top":      "#FBFBFB",
    "active_gradient_bottom":   "#FFFFFF",
    "hover_gradient_top":       "#DCDCDC",
    "hover_gradient_bottom":    "#F6F6F6",
    "active_bar_color":         "#6E9CBB",
}

# Environment variables for child shell
CHILD_ENV = {}

# Startup settings
STARTUP_DIRECTORY = os.path.expanduser("~")
# The directory exactly as it was written in the YAML ("~", "$HOME/src", ...).
# STARTUP_DIRECTORY above is the expanded, verified path used at spawn time;
# this one is what the settings dialog shows and what gets written back, so a
# "~" in the config file does not turn into "/home/user" on the first save.
STARTUP_DIRECTORY_RAW = "~"
STARTUP_MAXIMIZED = True
STARTUP_WINDOW_SIZE = None  # None = DE decides; (w, h) tuple = explicit size
# True = a new tab inherits the working directory of the tab it was opened
# from; False = every tab starts in STARTUP_DIRECTORY.
STARTUP_FOLLOW_CWD = False

# What a new tab (Ctrl+T) does.
#   "command" — run NEW_TAB_COMMAND straight away (the classic behaviour)
#   "picker"  — show the session picker and spawn only after the user chooses
NEW_TAB_ACTION = "command"
# Command line for the "command" action.  Empty = $SHELL as a login shell,
# which is what the terminal has always done.
NEW_TAB_COMMAND = ""

# Readline inputrc overrides (read from linux.yaml `inputrc:` section).
# Dict of readline variable → value, e.g. {"enable-active-region": "off"}.
# Set to False to disable custom inputrc entirely.
INPUTRC_OVERRIDES = {
    "enable-active-region": "off",
}

# Tab list overlay behavior
TAB_LIST_PREVIEW = True
TAB_LIST_ACTIVATE_ON_SELECT = True
TAB_LIST_SHOW_ON_CTRL_TAB = True
TAB_LIST_SHOW_ON_CTRL_TAB_DELAY = 200

# Debug logging
DEBUG_LOG = False

# Programs that use alt screen and will conflict with mc's Ctrl+O subshell.
_MC_SUBSHELL_DANGEROUS_CMDS = frozenset({
    "htop", "btop", "top", "atop", "glances", "nmon",
    "vim", "nvim", "vi", "nano", "micro", "mcedit",
    "less", "more", "most",
    "tmux", "screen", "byobu",
    "ranger", "nnn", "lf", "vifm",
    "mutt", "neomutt", "alpine",
    "irssi", "weechat",
    "ncdu", "tig", "lazygit", "lazydocker",
    "cmatrix", "dialog", "whiptail",
})

# AppImage/Qt environment variables to clean from child shell
_APPIMAGE_CLEAN_VARS = [
    "APPDIR", "APPIMAGE", "ARGV0", "OWD",
    "LD_LIBRARY_PATH",
    "QT_QPA_PLATFORM", "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_FONTDIR", "QML2_IMPORT_PATH",
    "QTWEBENGINE_CHROMIUM_FLAGS", "QTWEBENGINE_LOCALES_PATH",
    "TCL_LIBRARY", "TK_LIBRARY",
    "PYTHONIOENCODING", "PYTHONUNBUFFERED",
    "PYSIDE6_OPTION_PYTHON_ENUM",
    "GIT_TERMINAL_PROMPT", "SSH_ASKPASS_REQUIRE",
    "CRASH_LOG_FILE", "CRASH_LOG_PATH",
    "LOG_FILE", "LOG_ERROR_FILE",
    "PROMPT_EOL_MARK", "PROMPT_COMMAND", "TERM_SESSION_ID",
    "HISTFILE", "HISTSIZE", "HISTFILESIZE", "HISTCONTROL",
]
_APPIMAGE_TAINT_PREFIXES = (
    "QTMATERIAL_", "QT_QPA_", "QTWEBENGINE_",
    "QML", "PYSIDE", "NETERAGEN_",
    "primaryColor", "primaryLightColor", "primaryTextColor",
    "secondaryColor", "secondaryDarkColor", "secondaryLightColor",
    "secondaryTextColor",
)


# ---------------------------------------------------------------------------
# Config loading (reuses linux.yaml format from terminal.py)
# ---------------------------------------------------------------------------

def _hex_to_rgba(hex_str, alpha=1.0):
    """Parse '#RRGGBB' → Gdk.RGBA."""
    h = hex_str.strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = r, g, b, alpha
        return rgba
    except (ValueError, IndexError):
        return None


def expand_dir(text):
    """'~/src', '$HOME/src' → an absolute path.  Empty text → ''.

    The single place that turns a directory as written into a directory that
    can be chdir'ed to, so the loader and the settings dialog cannot disagree
    about which forms are accepted — a path the dialog took but the loader
    could not expand would silently stop working on the next start.
    """
    text = (text or "").strip()
    if not text:
        return ""
    return os.path.expanduser(os.path.expandvars(text))


def _load_yaml_config(filepath):
    """Load config from linux.yaml, updating globals."""
    global FONT_FAMILY, FONT_SIZE_PT, SCROLLBACK_LINES, BG_ALPHA
    global FG_COLOR, BG_COLOR, CURSOR_FG, CURSOR_BG
    global CHILD_ENV, STARTUP_DIRECTORY, STARTUP_MAXIMIZED, STARTUP_WINDOW_SIZE
    global STARTUP_DIRECTORY_RAW, STARTUP_FOLLOW_CWD
    global INPUTRC_OVERRIDES, DEBUG_LOG
    global TAB_LIST_PREVIEW, TAB_LIST_ACTIVATE_ON_SELECT
    global TAB_LIST_SHOW_ON_CTRL_TAB, TAB_LIST_SHOW_ON_CTRL_TAB_DELAY
    global NEW_TAB_ACTION, NEW_TAB_COMMAND
    global LOADED_CONFIG_PATH

    if not os.path.isfile(filepath):
        return
    LOADED_CONFIG_PATH = filepath

    with open(filepath, "r") as f:
        data = yaml.safe_load(f)
    if not data:
        return

    def _get_hex(section, key):
        v = section.get(key)
        if v and isinstance(v, str) and v.strip().startswith("#") and len(v.strip()) == 7:
            return v.strip()
        return None

    colors = data.get("colors", {})

    # Primary
    primary = colors.get("primary", {})
    bg = _get_hex(primary, "background")
    fg = _get_hex(primary, "foreground")
    if bg:
        BG_COLOR = bg
        CURSOR_FG = bg
    if fg:
        FG_COLOR = fg
        CURSOR_BG = fg

    # Cursor
    cursor_sec = colors.get("cursor", {})
    ct = _get_hex(cursor_sec, "text")
    cc = _get_hex(cursor_sec, "cursor")
    if ct:
        CURSOR_FG = ct
    if cc:
        CURSOR_BG = cc

    # Normal colors (0-7)
    _names = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
    normal = colors.get("normal", {})
    for i, name in enumerate(_names):
        c = _get_hex(normal, name)
        if c:
            PALETTE_HEX[i] = c

    # Bright colors (8-15)
    bright = colors.get("bright", {})
    for i, name in enumerate(_names):
        c = _get_hex(bright, name)
        if c:
            PALETTE_HEX[8 + i] = c

    # Tab bar colors
    tabs_cfg = colors.get("tabs", {})
    if isinstance(tabs_cfg, dict):
        _map = {
            "inactive_gradient_top":    "inactive_gradient_top",
            "inactive_gradient_bottom": "inactive_gradient_bottom",
            "active_gradient_top":      "active_gradient_top",
            "active_gradient_bottom":   "active_gradient_bottom",
            "hover_gradient_top":       "hover_gradient_top",
            "hover_gradient_bottom":    "hover_gradient_bottom",
            "active_bar_color":         "active_bar_color",
        }
        for yaml_key, theme_key in _map.items():
            v = _get_hex(tabs_cfg, yaml_key)
            if v:
                TAB_THEME[theme_key] = v

    # Font
    font_sec = data.get("font", {})
    font_normal = font_sec.get("normal", {})
    family = font_normal.get("family")
    if family:
        FONT_FAMILY = family
    size = font_sec.get("size")
    if size is not None:
        try:
            FONT_SIZE_PT = float(size)
        except (ValueError, TypeError):
            pass

    # Scrollback
    sb = data.get("scrollback_lines")
    if sb is not None:
        try:
            SCROLLBACK_LINES = max(0, int(sb))
        except (ValueError, TypeError):
            pass

    # Background opacity
    opacity = primary.get("background_opacity")
    if opacity is None:
        opacity = data.get("background_opacity")
    if opacity is not None:
        try:
            BG_ALPHA = max(0.0, min(1.0, float(opacity)))
        except (ValueError, TypeError):
            pass

    # Environment variables
    env_cfg = data.get("env")
    if isinstance(env_cfg, dict):
        for k, v in env_cfg.items():
            if v is None:
                CHILD_ENV[str(k)] = None
            else:
                CHILD_ENV[str(k)] = str(v)

    # Startup
    startup_cfg = data.get("startup", {})
    if isinstance(startup_cfg, dict):
        _sd = startup_cfg.get("directory")
        if _sd and isinstance(_sd, str):
            # The raw text is kept even when it does not resolve, so a typo is
            # visible in the settings dialog instead of silently reverting to
            # the home directory with no hint that the config was ignored.
            STARTUP_DIRECTORY_RAW = _sd.strip()
            _expanded = expand_dir(STARTUP_DIRECTORY_RAW)
            if _expanded and os.path.isdir(_expanded):
                STARTUP_DIRECTORY = _expanded
        _follow = startup_cfg.get("follow_cwd")
        if _follow is not None:
            STARTUP_FOLLOW_CWD = bool(_follow)
        _max = startup_cfg.get("maximized")
        if _max is not None:
            STARTUP_MAXIMIZED = bool(_max)
        _win_size = startup_cfg.get("window_size")
        if _win_size and isinstance(_win_size, str) and _win_size.lower() != "auto":
            try:
                _w, _h = _win_size.lower().split("x", 1)
                STARTUP_WINDOW_SIZE = (int(_w), int(_h))
            except (ValueError, TypeError):
                pass

    # New-tab behaviour
    new_tab_cfg = data.get("new_tab", {})
    if isinstance(new_tab_cfg, dict):
        _action = new_tab_cfg.get("action")
        if isinstance(_action, str) and _action.strip().lower() in ("command", "picker"):
            NEW_TAB_ACTION = _action.strip().lower()
        _cmd = new_tab_cfg.get("command")
        if isinstance(_cmd, str):
            NEW_TAB_COMMAND = _cmd.strip()

    # Debug logging
    _debug = data.get("debug")
    if _debug is not None:
        DEBUG_LOG = bool(_debug)

    # Tab list overlay behavior
    tab_list_cfg = data.get("tab_list", {})
    if isinstance(tab_list_cfg, dict):
        _preview = tab_list_cfg.get("preview")
        if _preview is not None:
            TAB_LIST_PREVIEW = bool(_preview)
        _activate = tab_list_cfg.get("activate_on_select")
        if _activate is not None:
            TAB_LIST_ACTIVATE_ON_SELECT = bool(_activate)
        _show_ctrl_tab = tab_list_cfg.get("show_on_ctrl_tab")
        if _show_ctrl_tab is not None:
            TAB_LIST_SHOW_ON_CTRL_TAB = bool(_show_ctrl_tab)
        _show_ctrl_tab_delay = tab_list_cfg.get("show_on_ctrl_tab_delay")
        if _show_ctrl_tab_delay is not None:
            TAB_LIST_SHOW_ON_CTRL_TAB_DELAY = int(_show_ctrl_tab_delay)

    # Readline inputrc overrides
    inputrc_cfg = data.get("inputrc")
    if inputrc_cfg is False:
        INPUTRC_OVERRIDES = False
    elif isinstance(inputrc_cfg, dict):
        INPUTRC_OVERRIDES = {}
        for k, v in inputrc_cfg.items():
            INPUTRC_OVERRIDES[str(k)] = str(v)


_ANSI_NAMES = ["black", "red", "green", "yellow",
               "blue", "magenta", "cyan", "white"]


def _save_yaml_config(path=None):
    """Serialize the current appearance globals back to YAML.

    Merges into the existing file (preserving unrelated sections such as
    inputrc); everything the Settings dialog can edit — colours, font,
    opacity, scrollback, env, startup, new_tab and tab_list — is overwritten.
    Returns the path written, or None on failure.
    Note: YAML comments are not preserved by the round-trip."""
    target = path or LOADED_CONFIG_PATH or DEFAULT_CONFIG_PATH

    data = {}
    if os.path.isfile(target):
        try:
            with open(target, "r") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            data = {}
    if not isinstance(data, dict):
        data = {}

    colors = data.setdefault("colors", {})
    if not isinstance(colors, dict):
        colors = data["colors"] = {}

    primary = colors.setdefault("primary", {})
    primary["background"] = BG_COLOR
    primary["foreground"] = FG_COLOR
    # Keep opacity in sync wherever the user already had it.
    if "background_opacity" in primary:
        primary["background_opacity"] = float(BG_ALPHA)

    cursor = colors.setdefault("cursor", {})
    cursor["text"] = CURSOR_FG
    cursor["cursor"] = CURSOR_BG

    normal = colors.setdefault("normal", {})
    bright = colors.setdefault("bright", {})
    for i, name in enumerate(_ANSI_NAMES):
        normal[name] = PALETTE_HEX[i]
        bright[name] = PALETTE_HEX[8 + i]

    font = data.setdefault("font", {})
    fnormal = font.setdefault("normal", {})
    fnormal["family"] = FONT_FAMILY
    font["size"] = (int(FONT_SIZE_PT)
                    if float(FONT_SIZE_PT).is_integer() else float(FONT_SIZE_PT))

    data["background_opacity"] = float(BG_ALPHA)
    data["scrollback_lines"] = int(SCROLLBACK_LINES)

    # startup/new_tab/tab_list are updated in place rather than replaced
    # wholesale, so a key the dialog does not expose (new_tab.command)
    # survives a save.
    startup = data.setdefault("startup", {})
    if not isinstance(startup, dict):
        startup = data["startup"] = {}
    startup["maximized"] = bool(STARTUP_MAXIMIZED)
    startup["window_size"] = ("auto" if not STARTUP_WINDOW_SIZE else
                              f"{STARTUP_WINDOW_SIZE[0]}x{STARTUP_WINDOW_SIZE[1]}")
    # The unexpanded form: writing STARTUP_DIRECTORY here would bake this
    # machine's home path into a config the user may well carry to another one.
    startup["directory"] = str(STARTUP_DIRECTORY_RAW)
    startup["follow_cwd"] = bool(STARTUP_FOLLOW_CWD)

    new_tab = data.setdefault("new_tab", {})
    if not isinstance(new_tab, dict):
        new_tab = data["new_tab"] = {}
    new_tab["action"] = str(NEW_TAB_ACTION)
    new_tab.setdefault("command", str(NEW_TAB_COMMAND))

    tab_list = data.setdefault("tab_list", {})
    if not isinstance(tab_list, dict):
        tab_list = data["tab_list"] = {}
    tab_list["preview"] = bool(TAB_LIST_PREVIEW)
    tab_list["activate_on_select"] = bool(TAB_LIST_ACTIVATE_ON_SELECT)
    tab_list["show_on_ctrl_tab"] = bool(TAB_LIST_SHOW_ON_CTRL_TAB)
    tab_list["show_on_ctrl_tab_delay"] = int(TAB_LIST_SHOW_ON_CTRL_TAB_DELAY)

    # Environment variables for the child shell.  CHILD_ENV maps name → value,
    # where None means "remove this inherited variable" (serialized as null).
    if CHILD_ENV:
        data["env"] = {str(k): (None if v is None else str(v))
                       for k, v in CHILD_ENV.items()}
    else:
        data.pop("env", None)

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False,
                           allow_unicode=True, default_flow_style=False)
    except OSError:
        return None
    return target

