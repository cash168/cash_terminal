"""Built-in colour scheme presets for the Settings dialog.

Each preset is a dict:
    name        : display name
    bg, fg      : primary background / foreground   (#RRGGBB)
    cursor_bg   : cursor colour                      (#RRGGBB)
    cursor_fg   : cursor text colour                 (#RRGGBB)
    normal      : 8 ANSI colours  0..7  (black red green yellow blue magenta cyan white)
    bright      : 8 ANSI colours  8..15 (bright variants, same order)

PALETTE_HEX in config is normal + bright (16 entries).
"""

PRESETS = [
    {
        "name": "Konsole Linux Colors",
        "bg": "#000000", "fg": "#B2B2B2",
        "cursor_bg": "#B2B2B2", "cursor_fg": "#000000",
        "normal": ["#000000", "#B21818", "#18B218", "#B26818",
                   "#1818B2", "#B218B2", "#18B2B2", "#B2B2B2"],
        "bright": ["#7E7E7E", "#FF5454", "#54FF54", "#FFFF54",
                   "#5454FF", "#FF54FF", "#54FFFF", "#FFFFFF"],
    },
    {
        "name": "Solarized Dark",
        "bg": "#002B36", "fg": "#839496",
        "cursor_bg": "#839496", "cursor_fg": "#002B36",
        "normal": ["#073642", "#DC322F", "#859900", "#B58900",
                   "#268BD2", "#D33682", "#2AA198", "#EEE8D5"],
        "bright": ["#002B36", "#CB4B16", "#586E75", "#657B83",
                   "#839496", "#6C71C4", "#93A1A1", "#FDF6E3"],
    },
    {
        "name": "Solarized Light",
        "bg": "#FDF6E3", "fg": "#657B83",
        "cursor_bg": "#657B83", "cursor_fg": "#FDF6E3",
        "normal": ["#073642", "#DC322F", "#859900", "#B58900",
                   "#268BD2", "#D33682", "#2AA198", "#EEE8D5"],
        "bright": ["#002B36", "#CB4B16", "#586E75", "#657B83",
                   "#839496", "#6C71C4", "#93A1A1", "#FDF6E3"],
    },
    {
        "name": "Dracula",
        "bg": "#282A36", "fg": "#F8F8F2",
        "cursor_bg": "#F8F8F2", "cursor_fg": "#282A36",
        "normal": ["#21222C", "#FF5555", "#50FA7B", "#F1FA8C",
                   "#BD93F9", "#FF79C6", "#8BE9FD", "#F8F8F2"],
        "bright": ["#6272A4", "#FF6E6E", "#69FF94", "#FFFFA5",
                   "#D6ACFF", "#FF92DF", "#A4FFFF", "#FFFFFF"],
    },
    {
        "name": "Gruvbox Dark",
        "bg": "#282828", "fg": "#EBDBB2",
        "cursor_bg": "#EBDBB2", "cursor_fg": "#282828",
        "normal": ["#282828", "#CC241D", "#98971A", "#D79921",
                   "#458588", "#B16286", "#689D6A", "#A89984"],
        "bright": ["#928374", "#FB4934", "#B8BB26", "#FABD2F",
                   "#83A598", "#D3869B", "#8EC07C", "#EBDBB2"],
    },
    {
        "name": "One Dark",
        "bg": "#282C34", "fg": "#ABB2BF",
        "cursor_bg": "#ABB2BF", "cursor_fg": "#282C34",
        "normal": ["#282C34", "#E06C75", "#98C379", "#E5C07B",
                   "#61AFEF", "#C678DD", "#56B6C2", "#ABB2BF"],
        "bright": ["#5A6374", "#E06C75", "#98C379", "#E5C07B",
                   "#61AFEF", "#C678DD", "#56B6C2", "#FFFFFF"],
    },
]


def preset_names():
    return [p["name"] for p in PRESETS]


def get_preset(name):
    for p in PRESETS:
        if p["name"] == name:
            return p
    return None


def match_current(bg, fg, palette_hex):
    """Return the name of the preset matching the given colours, or None.

    Comparison is case-insensitive on the hex strings. palette_hex is the
    16-entry normal+bright list."""
    def norm(s):
        return (s or "").strip().lower()

    pal = [norm(h) for h in (palette_hex or [])]
    for p in PRESETS:
        want = [norm(h) for h in (p["normal"] + p["bright"])]
        if (norm(p["bg"]) == norm(bg) and norm(p["fg"]) == norm(fg)
                and pal[:16] == want):
            return p["name"]
    return None
