"""CashTerminal — a Vte.Terminal-replacement widget backed by cashterm_core.

The Rust core (cashterm_core.PtyTerm) owns the PTY + alacritty parser + grid.
This widget renders the visible grid with Cairo, translates keystrokes to PTY
bytes, drives mouse selection / xterm mouse reporting, and exposes the slice of
the old Vte.Terminal API that the rest of cash_terminal/ already consumes, so
tab.py / search.py / paste.py / interaction.py / app.py need only small edits.

Rendering, input and mouse handling grew out of the cashterm_core mini-host
prototype, which has since been deleted — this file is now the only
implementation.  Colors/font come from cash_terminal.config.
"""
import math
import struct
import time

from gi.repository import Gtk, Gdk, GLib, GObject, Pango, PangoCairo  # noqa: E402
import cairo  # noqa: E402

import cashterm_core  # noqa: E402  (the Rust extension built by maturin)

from . import config
from .util import _translate_keyval_to_latin


# ---------------------------------------------------------------------------
# Snapshot wire format (must match cashterm_core/src/lib.rs exactly):
#   rows*cols cells, 16 bytes/cell little-endian, 4x u32: [cp][fg][bg][flags]
#   color u32 = (tag << 24) | value:
#     tag 0 -> default fg, tag 1 -> default bg,
#     tag 2 -> palette index (value 0..255), tag 3 -> RGB (value 0xRRGGBB)
# ---------------------------------------------------------------------------
CELL_STRUCT = struct.Struct("<4I")

# Flag bits (must match encode_flags in lib.rs)
F_BOLD = 1
F_ITALIC = 2
F_UNDERLINE = 4
F_INVERSE = 8
F_STRIKE = 16
F_DIM = 32
F_WIDE = 64
F_WIDE_SPACER = 128


# ---------------------------------------------------------------------------
# Box-drawing / block elements rendered as crisp vector shapes instead of font
# glyphs.  Font glyphs for U+2500..U+257F rarely fill the cell to its edges, so
# stacked vertical rules (e.g. mc panel borders) show gaps between rows.  We
# draw the line segments ourselves so they join seamlessly across cells.
#
# Each box-drawing entry maps codepoint -> (up, down, left, right) where the
# value is the stroke weight toward that edge: 0 none, 1 light, 2 heavy, 3 double.
# ---------------------------------------------------------------------------
_BOX_SEGMENTS = {
    # light lines / corners / tees / cross
    0x2500: (0, 0, 1, 1), 0x2502: (1, 1, 0, 0),
    0x250C: (0, 1, 0, 1), 0x2510: (0, 1, 1, 0),
    0x2514: (1, 0, 0, 1), 0x2518: (1, 0, 1, 0),
    0x251C: (1, 1, 0, 1), 0x2524: (1, 1, 1, 0),
    0x252C: (0, 1, 1, 1), 0x2534: (1, 0, 1, 1), 0x253C: (1, 1, 1, 1),
    # heavy lines / corners / tees / cross
    0x2501: (0, 0, 2, 2), 0x2503: (2, 2, 0, 0),
    0x250F: (0, 2, 0, 2), 0x2513: (0, 2, 2, 0),
    0x2517: (2, 0, 0, 2), 0x251B: (2, 0, 2, 0),
    0x2523: (2, 2, 0, 2), 0x252B: (2, 2, 2, 0),
    0x2533: (0, 2, 2, 2), 0x253B: (2, 0, 2, 2), 0x254B: (2, 2, 2, 2),
    # double lines / corners / tees / cross
    0x2550: (0, 0, 3, 3), 0x2551: (3, 3, 0, 0),
    0x2554: (0, 3, 0, 3), 0x2557: (0, 3, 3, 0),
    0x255A: (3, 0, 0, 3), 0x255D: (3, 0, 3, 0),
    0x2560: (3, 3, 0, 3), 0x2563: (3, 3, 3, 0),
    0x2566: (0, 3, 3, 3), 0x2569: (3, 0, 3, 3), 0x256C: (3, 3, 3, 3),
    # mixed single/double corners + tees (common in TUI frames)
    0x2552: (0, 1, 0, 3), 0x2553: (0, 3, 0, 1),
    0x2555: (0, 1, 3, 0), 0x2556: (0, 3, 1, 0),
    0x2558: (1, 0, 0, 3), 0x2559: (3, 0, 0, 1),
    0x255B: (1, 0, 3, 0), 0x255C: (3, 0, 1, 0),
    0x255E: (1, 1, 0, 3), 0x255F: (3, 3, 0, 1),
    0x2561: (1, 1, 3, 0), 0x2562: (3, 3, 1, 0),
    0x2564: (0, 1, 3, 3), 0x2565: (0, 3, 1, 1),
    0x2567: (1, 0, 3, 3), 0x2568: (3, 0, 1, 1),
    0x256A: (1, 1, 3, 3), 0x256B: (3, 3, 1, 1),
}

# Quadrant decomposition for block-element glyphs that are unions of the four
# cell quadrants: (top-left, top-right, bottom-left, bottom-right) as booleans.
_BOX_QUADRANTS = {
    0x2580: (1, 1, 0, 0),  # upper half
    0x2584: (0, 0, 1, 1),  # lower half
    0x258C: (1, 0, 1, 0),  # left half
    0x2590: (0, 1, 0, 1),  # right half
    0x2588: (1, 1, 1, 1),  # full block
    0x2596: (0, 0, 1, 0), 0x2597: (0, 0, 0, 1),
    0x2598: (1, 0, 0, 0), 0x2599: (1, 0, 1, 1),
    0x259A: (1, 0, 0, 1), 0x259B: (1, 1, 1, 0),
    0x259C: (1, 1, 0, 1), 0x259D: (0, 1, 0, 0),
    0x259E: (0, 1, 1, 0), 0x259F: (0, 1, 1, 1),
}

# Shade blocks: fill the whole cell with the foreground at this opacity.
_BOX_SHADES = {0x2591: 0.25, 0x2592: 0.5, 0x2593: 0.75}


def _hex_to_rgb(hex_str, fallback=(0.0, 0.0, 0.0)):
    """'#RRGGBB' -> (r, g, b) floats 0..1."""
    try:
        h = hex_str.strip().lstrip("#")
        if len(h) != 6:
            return fallback
        return (int(h[0:2], 16) / 255.0,
                int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0)
    except (ValueError, AttributeError, IndexError):
        return fallback


def _rgba_to_rgb(rgba):
    """Gdk.RGBA -> (r, g, b) floats."""
    return (rgba.red, rgba.green, rgba.blue)


# ---------------------------------------------------------------------------
# Keyboard: GTK keyval/state -> bytes for the PTY.
# ---------------------------------------------------------------------------
CURSOR_KEYS = {
    Gdk.KEY_Up: b"A",
    Gdk.KEY_Down: b"B",
    Gdk.KEY_Right: b"C",
    Gdk.KEY_Left: b"D",
    Gdk.KEY_Home: b"H",
    Gdk.KEY_End: b"F",
}

SPECIAL_KEYS = {
    Gdk.KEY_Return: b"\r",
    Gdk.KEY_KP_Enter: b"\r",
    Gdk.KEY_BackSpace: b"\x7f",
    Gdk.KEY_Tab: b"\t",
    Gdk.KEY_ISO_Left_Tab: b"\x1b[Z",
    Gdk.KEY_Escape: b"\x1b",
    Gdk.KEY_Insert: b"\x1b[2~",
    Gdk.KEY_Delete: b"\x1b[3~",
    Gdk.KEY_Page_Up: b"\x1b[5~",
    Gdk.KEY_Page_Down: b"\x1b[6~",
    Gdk.KEY_F1: b"\x1bOP",
    Gdk.KEY_F2: b"\x1bOQ",
    Gdk.KEY_F3: b"\x1bOR",
    Gdk.KEY_F4: b"\x1bOS",
    Gdk.KEY_F5: b"\x1b[15~",
    Gdk.KEY_F6: b"\x1b[17~",
    Gdk.KEY_F7: b"\x1b[18~",
    Gdk.KEY_F8: b"\x1b[19~",
    Gdk.KEY_F9: b"\x1b[20~",
    Gdk.KEY_F10: b"\x1b[21~",
    Gdk.KEY_F11: b"\x1b[23~",
    Gdk.KEY_F12: b"\x1b[24~",
}

KP_NORMALIZE = {
    Gdk.KEY_KP_Up: Gdk.KEY_Up,
    Gdk.KEY_KP_Down: Gdk.KEY_Down,
    Gdk.KEY_KP_Left: Gdk.KEY_Left,
    Gdk.KEY_KP_Right: Gdk.KEY_Right,
    Gdk.KEY_KP_Home: Gdk.KEY_Home,
    Gdk.KEY_KP_End: Gdk.KEY_End,
    Gdk.KEY_KP_Page_Up: Gdk.KEY_Page_Up,
    Gdk.KEY_KP_Page_Down: Gdk.KEY_Page_Down,
    Gdk.KEY_KP_Insert: Gdk.KEY_Insert,
    Gdk.KEY_KP_Delete: Gdk.KEY_Delete,
}


def keyval_to_bytes(keyval, state, app_cursor, keycode=None):
    ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
    alt = bool(state & Gdk.ModifierType.ALT_MASK)

    keyval = KP_NORMALIZE.get(keyval, keyval)

    fin = CURSOR_KEYS.get(keyval)
    if fin is not None:
        seq = (b"\x1bO" if app_cursor else b"\x1b[") + fin
        return (b"\x1b" + seq) if alt else seq

    special = SPECIAL_KEYS.get(keyval)
    if special is not None:
        return (b"\x1b" + special) if alt else special

    uni = Gdk.keyval_to_unicode(keyval)
    ch = chr(uni) if uni else ""

    if ctrl:
        lo = ch.lower()
        # Non-Latin layout (e.g. Russian): the keyval is a Cyrillic letter, so
        # Ctrl+R would send "к" instead of \x12.  Translate the physical key to
        # its Latin equivalent and derive the control byte from that.
        if not ("a" <= lo <= "z") and keycode is not None:
            lat_uni = Gdk.keyval_to_unicode(
                _translate_keyval_to_latin(keycode, keyval))
            if lat_uni:
                lat_ch = chr(lat_uni)
                if "a" <= lat_ch.lower() <= "z":
                    ch = lat_ch
                    lo = lat_ch.lower()
        if "a" <= lo <= "z":
            return bytes([ord(lo) - ord("a") + 1])
        ctrl_map = {
            " ": b"\x00", "@": b"\x00", "[": b"\x1b", "\\": b"\x1c",
            "]": b"\x1d", "^": b"\x1e", "_": b"\x1f", "?": b"\x7f",
        }
        if ch in ctrl_map:
            return ctrl_map[ch]

    if uni == 0:
        return None

    if uni < 0x20:
        return None

    data = ch.encode("utf-8")
    return (b"\x1b" + data) if alt else data


class _PtyShim:
    """Minimal stand-in for Vte.Pty: only get_fd() is used by the app
    (TIOCGPGRP for foreground-process detection, direct os.write for paste
    and readline cursor moves)."""
    __slots__ = ("_fd",)

    def __init__(self, fd):
        self._fd = fd

    def get_fd(self):
        return self._fd


class CashTerminal(Gtk.DrawingArea):
    """Drop-in terminal widget backed by the Rust core.

    Emits the subset of Vte.Terminal signals the app connects to:
      * child-exited(status)
      * window-title-changed()
      * cursor-moved()
      * contents-changed()
    """

    __gsignals__ = {
        "child-exited": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "window-title-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "cursor-moved": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "contents-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Right-click requested a context menu at widget coords (x, y).
        "context-menu": (GObject.SignalFlags.RUN_LAST, None, (float, float)),
    }

    def __init__(self):
        super().__init__()
        self.term = None  # set by spawn()
        self.cols = 80
        self.rows = 24

        # Font / cell metrics.  Glyphs are rendered through Pango (proper font
        # fallback for powerline/nerd/CJK/emoji + hinting), cached per
        # (codepoint, bold, italic) as A8 alpha masks and tinted to the cell's
        # foreground colour at blit time.
        self._font_family = config.FONT_FAMILY
        self._font_px = config.FONT_SIZE_PT * 96.0 / 72.0  # pt -> px @96dpi
        self._pango_ctx = PangoCairo.FontMap.get_default().create_context()
        self.cw, self.ch, self.baseline = self._measure_cell()

        # Color tables (built from config; overridable via set_colors()).
        self._build_palette_from_config()
        self._clear_background = True  # set_clear_background(False) for transparency
        # Alpha applied to the DEFAULT background only (window transparency).
        # Cell-specific (non-default) backgrounds always stay opaque.
        try:
            self._def_bg_a = float(config.BG_ALPHA)
        except (TypeError, ValueError):
            self._def_bg_a = 1.0
        # Konsole/xterm default: bold text with an ANSI palette colour (0..7)
        # is drawn in the matching bright colour (8..15).  tab.py requests this.
        self._bold_is_bright = True

        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(True)
        self.set_draw_func(self._draw)
        self.connect("resize", self._on_resize)

        # Scroll adjustment driving the ScrolledWindow / scrollbar. Absolute
        # row space: value = top visible row, upper = history+rows, page = rows.
        self._vadj = Gtk.Adjustment(
            value=0, lower=0, upper=self.rows, step_increment=1,
            page_increment=self.rows, page_size=self.rows,
        )
        self._vadj.connect("value-changed", self._on_vadj_changed)
        self._sync_adj = False  # guard against scroll<->core feedback

        # Persistent render buffer + per-row diff (htop-refresh fix).
        self._glyph_cache = {}
        self._screen = None
        self._screen_dims = None
        self._prev_snap = None
        self._frame_snap = None  # snapshot fetched in _tick, reused in _draw

        # Scrollback cap handed to the Rust grid at spawn() time; overridden by
        # set_scrollback_lines() (tab.py calls it with config.SCROLLBACK_LINES).
        self._scrollback = config.SCROLLBACK_LINES

        # Search highlights are not cached here: _draw asks the core for the
        # matches intersecting the current viewport on every frame.

        # Mouse state.
        self._sel_active = False
        self._mouse_report_btn = None
        self._ptr_cell = (0, 0)
        self._last_report_cell = None

        # Pointer cursor: I-beam ("text") when selection is possible (normal
        # shell mode, or any mode while Shift is held), default arrow when a
        # full-screen app is grabbing the mouse (mc, vim, …).
        self._cursor_text = Gdk.Cursor.new_from_name("text", None)
        self._cursor_default = Gdk.Cursor.new_from_name("default", None)
        self._cur_cursor_is_text = None   # last applied (None = not yet set)
        self._shift_held = False

        # Cursor blink + change tracking for signals.
        self._blink_on = True
        self._last_blink = time.monotonic()
        self._cur_title = None
        self._last_cursor = None
        self._exited = False
        self._tick_id = None

        self._install_controllers()

    # ----------------------------------------------------------- spawn
    def spawn(self, shell, argv=None, cwd=None, env=None):
        """Create the Rust core + shell. argv excludes the program name
        (e.g. ['-l']); env is a list of 'KEY=VALUE' strings (Vte-style)."""
        args = list(argv) if argv else []
        env_pairs = []
        for item in (env or []):
            if "=" in item:
                k, v = item.split("=", 1)
                env_pairs.append((k, v))
        # Size the grid from the current allocation before the PTY is created.
        # self.cols/self.rows still hold the 80x24 placeholder unless a resize
        # has been processed, and "resize" is dropped while self.term is None
        # (see _on_resize).  A tab that showed the session picker first is
        # already laid out by the time it spawns, so without this the child
        # would be handed 80x24 for a full-size window — and since no further
        # resize follows, mc and friends would stay that size forever.
        alloc_w, alloc_h = self.get_width(), self.get_height()
        if alloc_w > 0 and alloc_h > 0 and self.cw > 0 and self.ch > 0:
            self.cols = max(1, int(alloc_w // self.cw))
            self.rows = max(1, int(alloc_h // self.ch))

        sb = getattr(self, "_scrollback", config.SCROLLBACK_LINES)
        try:
            self.term = cashterm_core.PtyTerm(
                self.rows, self.cols, shell, args, cwd, env_pairs, sb)
        except TypeError:
            # Core built before the `scrollback` parameter existed — fall back
            # to its hardcoded 10 000 lines rather than failing to spawn.
            self.term = cashterm_core.PtyTerm(
                self.rows, self.cols, shell, args, cwd, env_pairs)
        self._refresh_adjustment()
        if self._tick_id is None:
            self._tick_id = self.add_tick_callback(self._tick)
        return self.term.pid()

    # ----------------------------------------------------------- controllers
    def _install_controllers(self):
        keyctl = Gtk.EventControllerKey()
        keyctl.connect("key-pressed", self._on_key)
        # Track Shift press/release so the pointer cursor can switch to the
        # I-beam in app-mouse mode while Shift is held (mc, vim, …).
        try:
            keyctl.connect("modifiers", self._on_modifiers)
        except TypeError:
            pass
        self.add_controller(keyctl)

        scrollctl = Gtk.EventControllerScroll()
        scrollctl.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
        scrollctl.connect("scroll", self._on_wheel)
        self.add_controller(scrollctl)

        clickctl = Gtk.GestureClick()
        clickctl.set_button(0)
        clickctl.connect("pressed", self._on_click_pressed)
        clickctl.connect("released", self._on_click_released)
        self.add_controller(clickctl)

        motionctl = Gtk.EventControllerMotion()
        motionctl.connect("motion", self._on_motion)
        self.add_controller(motionctl)

    # ----------------------------------------------------------- palette
    def _build_palette_from_config(self):
        self._def_fg = _hex_to_rgb(config.FG_COLOR, (0.83, 0.84, 0.85))
        self._def_bg = _hex_to_rgb(config.BG_COLOR, (0.0, 0.0, 0.0))
        ansi = [_hex_to_rgb(h) for h in config.PALETTE_HEX]
        while len(ansi) < 16:
            ansi.append((0.5, 0.5, 0.5))
        self._ansi = ansi[:16]
        self._xterm = self._build_xterm256(self._ansi)
        self._cursor_bg = _hex_to_rgb(config.CURSOR_BG, (0.83, 0.84, 0.85))
        self._cursor_fg = _hex_to_rgb(config.CURSOR_FG, (0.0, 0.0, 0.0))

    @staticmethod
    def _build_xterm256(ansi16):
        pal = [(0.0, 0.0, 0.0)] * 256
        for i in range(16):
            pal[i] = ansi16[i]
        levels = [0, 95, 135, 175, 215, 255]
        idx = 16
        for r in range(6):
            for g in range(6):
                for b in range(6):
                    pal[idx] = (levels[r] / 255.0, levels[g] / 255.0,
                                levels[b] / 255.0)
                    idx += 1
        for i in range(24):
            v = (8 + i * 10) / 255.0
            pal[232 + i] = (v, v, v)
        return pal

    def _bright_if_bold(self, packed, bold):
        """Promote an ANSI palette colour 0..7 to its bright 8..15 variant for
        bold cells (Konsole/xterm 'bold is bright')."""
        if not (bold and self._bold_is_bright):
            return packed
        if (packed >> 24) & 0xFF == 2:           # tag 2 = palette index
            val = packed & 0xFFFFFF
            if val < 8:
                return (2 << 24) | (val + 8)
        return packed

    def _resolve_color(self, packed, is_fg):
        tag = (packed >> 24) & 0xFF
        val = packed & 0xFFFFFF
        if tag == 0:
            return self._def_fg
        if tag == 1:
            return self._def_bg
        if tag == 2:
            return self._xterm[val & 0xFF]
        if tag == 3:
            return ((val >> 16 & 0xFF) / 255.0, (val >> 8 & 0xFF) / 255.0,
                    (val & 0xFF) / 255.0)
        return self._def_fg if is_fg else self._def_bg

    # ----------------------------------------------------------- font metrics
    def _base_font_desc(self, bold=False, italic=False):
        desc = Pango.FontDescription()
        desc.set_family(self._font_family)
        desc.set_absolute_size(self._font_px * Pango.SCALE)
        desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        desc.set_style(Pango.Style.ITALIC if italic else Pango.Style.NORMAL)
        return desc

    def _measure_cell(self):
        """Cell size from Pango font metrics (matches the glyph renderer)."""
        desc = self._base_font_desc()
        metrics = self._pango_ctx.get_metrics(desc, None)
        ascent = metrics.get_ascent() / Pango.SCALE
        descent = metrics.get_descent() / Pango.SCALE
        cw = metrics.get_approximate_char_width() / Pango.SCALE
        # Guard against fonts whose 'M' is wider than the approximate advance.
        layout = Pango.Layout.new(self._pango_ctx)
        layout.set_font_description(desc)
        layout.set_text("M", -1)
        mw, _mh = layout.get_pixel_size()
        cw = max(cw, float(mw))
        height = ascent + descent
        return cw, height, ascent

    def _glyph_mask(self, cp, bold, italic):
        """Return (A8_surface, cell_span) for a codepoint, rendered + cached.

        The mask holds the glyph coverage as alpha; the caller tints it to the
        desired colour via mask_surface().  cell_span is 1 or 2 (wide glyphs)."""
        key = (cp, bold, italic)
        cached = self._glyph_cache.get(key)
        if cached is not None:
            return cached

        layout = Pango.Layout.new(self._pango_ctx)
        layout.set_font_description(self._base_font_desc(bold, italic))
        try:
            layout.set_text(chr(cp), -1)
        except (ValueError, OverflowError):
            layout.set_text(" ", -1)

        lw, _lh = layout.get_pixel_size()
        span = 2 if lw > self.cw * 1.4 else 1
        surf_w = max(1, int(math.ceil(self.cw * span)))
        surf_h = max(1, int(math.ceil(self.ch)))
        surf = cairo.ImageSurface(cairo.FORMAT_A8, surf_w, surf_h)
        mcr = cairo.Context(surf)
        # Align the glyph baseline to the cell baseline.
        voff = self.baseline - (layout.get_baseline() / Pango.SCALE)
        mcr.move_to(0, voff)
        mcr.set_source_rgba(0, 0, 0, 1)  # A8: only alpha (=coverage) is kept
        PangoCairo.show_layout(mcr, layout)
        surf.flush()

        result = (surf, span)
        self._glyph_cache[key] = result
        return result

    # =======================================================================
    #  Vte.Terminal-compatible API (the slice the app uses)
    # =======================================================================
    def get_pty(self):
        if self.term is None:
            return None
        fd = self.term.pty_fd()
        return _PtyShim(fd) if fd >= 0 else None

    def get_vadjustment(self):
        return self._vadj

    def get_column_count(self):
        return self.cols

    def get_row_count(self):
        return self.rows

    def get_char_width(self):
        return self.cw

    def get_char_height(self):
        return self.ch

    def get_cursor_position(self):
        """Return (col, abs_row) — column and ABSOLUTE row (0 = top of
        scrollback), matching Vte.Terminal.get_cursor_position semantics used
        across the app."""
        if self.term is None:
            return (0, 0)
        try:
            vrow, col, _vis = self.term.cursor()
            hist = self.term.history_size()
            off = self.term.display_offset()
        except Exception:
            return (0, 0)
        return (col, (hist - off) + vrow)

    def get_text_range_format(self, _fmt, start_row, _start_col, end_row, _end_col):
        """Absolute-row text extraction (Vte signature: fmt, sr, sc, er, ec).
        Returns a plain string. Columns are ignored — full rows are returned."""
        if self.term is None:
            return ""
        try:
            return self.term.text_abs(int(start_row), int(end_row))
        except Exception:
            return ""

    def get_has_selection(self):
        if self.term is None:
            return False
        try:
            return bool(self.term.selection_text())
        except Exception:
            return False

    def get_text_selected(self, _fmt=None):
        if self.term is None:
            return ""
        try:
            return self.term.selection_text()
        except Exception:
            return ""

    def copy_clipboard_format(self, _fmt=None):
        """Copy the active selection to the system clipboard."""
        text = self.get_text_selected()
        if text:
            self.get_clipboard().set(text)

    def select_all(self):
        """Select the entire buffer (scrollback top → bottom of screen)."""
        if self.term is None:
            return
        try:
            hist = self.term.history_size()
            disp = self.term.display_offset()
        except Exception:
            return
        try:
            # selection rows are viewport-relative: Line = row - display_offset.
            # row = disp - hist  -> Line(-hist) (top of scrollback)
            # row = disp + rows-1 -> Line(rows-1) (bottom visible row)
            self.term.selection_start(0, disp - hist, False, "simple")
            self.term.selection_update(max(0, self.cols - 1),
                                       disp + (self.rows - 1), True)
        except Exception:
            return
        self._sel_active = False
        self.queue_draw()

    def clear_screen(self):
        """Clear the screen (sends Ctrl+L; shells/readline clear, TUIs redraw)."""
        if self.term is None:
            return
        try:
            self.term.scroll_to_bottom()
            self.term.feed_input(b"\x0c")
        except Exception:
            pass

    # --- search (delegated to the core's alacritty RegexSearch) -------------
    def set_search(self, pattern):
        """Install a search pattern in the core. Returns True when usable.

        `pattern` is a regex.  Callers that want a literal search must escape
        it first (see search.py:_escape_literal).  Matching is smart-case: an
        all-lowercase pattern is case-insensitive, any uppercase makes it
        case-sensitive."""
        if self.term is None:
            return False
        try:
            ok = bool(self.term.set_search(pattern or ""))
        except Exception:
            return False
        self.queue_draw()
        return ok

    def clear_search(self):
        """Drop the active search and its highlights."""
        if self.term is None:
            return
        try:
            self.term.clear_search()
        except Exception:
            pass
        self.queue_draw()

    def search_next(self, reverse, origin_row=None, origin_col=None):
        """Advance to the next match; returns its absolute (row, col) or None.

        Does not scroll — the caller decides where to place the match.
        `origin_row`/`origin_col` say where the walk starts, in absolute
        coordinates.  Passing both steps one cell past that position, so the
        match sitting there is not returned again; passing only the row starts
        from the row edge, which keeps a match on that row reachable.  Without
        either, the core continues from its own last focus — which is unsafe
        while output streams, because that focus is stored in grid coordinates
        and slides as new lines push the content up."""
        if self.term is None:
            return None
        try:
            res = self.term.search_next(bool(reverse), origin_row, origin_col)
        except Exception:
            return None
        self.queue_draw()
        # An un-rebuilt core still returns a plain bool from search_next; treat
        # that as "no usable position" rather than mis-reading True as a row.
        if isinstance(res, tuple) and len(res) == 2:
            return res
        return None

    def has_visible_match(self):
        """True when at least one match lies in the current viewport."""
        if self.term is None:
            return False
        try:
            return bool(self.term.search_visible())
        except Exception:
            return False

    def feed_child(self, data):
        """Send bytes to the PTY (Vte.Terminal.feed_child equivalent)."""
        if self.term is None:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        try:
            self.term.feed_input(bytes(data))
        except Exception:
            pass

    # --- color / font setters (Vte.Terminal-compatible; store + repaint) ---
    def set_colors(self, fg, bg, palette):
        if fg is not None:
            self._def_fg = _rgba_to_rgb(fg)
        if bg is not None:
            self._def_bg = _rgba_to_rgb(bg)
            self._def_bg_a = float(bg.alpha)
        if palette and len(palette) >= 16:
            self._ansi = [_rgba_to_rgb(c) for c in palette[:16]]
            self._xterm = self._build_xterm256(self._ansi)
        self._invalidate_render()

    def set_color_foreground(self, rgba):
        if rgba is not None:
            self._def_fg = _rgba_to_rgb(rgba)
            self._invalidate_render()

    def set_color_background(self, rgba):
        if rgba is not None:
            self._def_bg = _rgba_to_rgb(rgba)
            self._def_bg_a = float(rgba.alpha)
            self._invalidate_render()

    def set_color_cursor(self, rgba):
        if rgba is not None:
            self._cursor_bg = _rgba_to_rgb(rgba)
            self.queue_draw()

    def set_color_cursor_foreground(self, rgba):
        if rgba is not None:
            self._cursor_fg = _rgba_to_rgb(rgba)
            self.queue_draw()

    def set_clear_background(self, clear):
        self._clear_background = bool(clear)
        self.queue_draw()

    def set_font(self, font_desc):
        if font_desc is None:
            return
        fam = font_desc.get_family()
        if fam:
            self._font_family = fam
        size = font_desc.get_size()
        if size > 0:
            if font_desc.get_size_is_absolute():
                self._font_px = size / Pango.SCALE
            else:
                self._font_px = (size / Pango.SCALE) * 96.0 / 72.0
        self.cw, self.ch, self.baseline = self._measure_cell()
        self._glyph_cache.clear()
        self._reflow_to_size()
        self._invalidate_render()

    # --- accepted-but-handled-internally Vte setters (no-ops / stored) ---
    def set_scrollback_lines(self, n):
        """Store the scrollback cap; applied when the core is spawned.

        The cap lives in the Rust grid (alacritty's `scrolling_history`), which
        is fixed at construction time, so this only has an effect when called
        before spawn() — which is what tab.py does."""
        try:
            self._scrollback = max(0, int(n))
        except (TypeError, ValueError):
            pass

    def set_scroll_on_output(self, _v):
        pass

    def set_scroll_on_keystroke(self, _v):
        pass

    def set_cursor_shape(self, _shape):
        pass

    def set_cursor_blink_mode(self, _mode):
        pass

    def set_bold_is_bright(self, v):
        self._bold_is_bright = bool(v)
        self._invalidate_render()

    def set_mouse_autohide(self, _v):
        pass

    def _invalidate_render(self):
        self._prev_snap = None  # force full repaint with new colors
        self.queue_draw()

    # =======================================================================
    #  Scroll adjustment <-> core
    # =======================================================================
    def scroll_to_bottom(self):
        """Scroll the viewport to the bottom (the input cursor / newest output).

        Called e.g. after a paste so the pasted text at the prompt is visible
        even when the user had scrolled up into the scrollback.  Works whether
        or not output is currently streaming."""
        if self.term is None:
            return
        try:
            self.term.scroll_to_bottom()
        except Exception:
            return
        self._refresh_adjustment()
        self.queue_draw()

    def _refresh_adjustment(self):
        if self.term is None:
            return
        try:
            hist = self.term.history_size()
            off = self.term.display_offset()
        except Exception:
            return
        self._sync_adj = True
        self._vadj.set_upper(hist + self.rows)
        self._vadj.set_page_size(self.rows)
        self._vadj.set_page_increment(self.rows)
        self._vadj.set_value(hist - off)
        self._sync_adj = False

    def _on_vadj_changed(self, adj):
        if self._sync_adj or self.term is None:
            return
        try:
            hist = self.term.history_size()
            cur_off = self.term.display_offset()
        except Exception:
            return
        want_off = max(0, min(hist, int(round(hist - adj.get_value()))))
        delta = want_off - cur_off
        if delta != 0:
            self.term.scroll_lines(delta)
            self.queue_draw()

    def _on_wheel(self, controller, dx, dy):
        if self.term is None:
            return False
        state = controller.get_current_event_state()
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if self._app_mouse() and not shift:
            col, row = self._ptr_cell
            btn = 64 if dy < 0 else 65
            mods = self._mods_bits(state)
            for _ in range(max(1, int(round(abs(dy))))):
                data = self.term.mouse_report(col, row, btn, 0, mods)
                if data:
                    self.term.feed_input(bytes(data))
            return True
        lines = int(-dy) * 3
        if lines == 0:
            lines = -3 if dy > 0 else 3
        self.term.scroll_lines(lines)
        self._refresh_adjustment()
        self.queue_draw()
        return True

    # =======================================================================
    #  Mouse: selection + xterm reporting
    # =======================================================================
    def _xy_to_cell(self, x, y):
        col = max(0, min(self.cols - 1, int(x // self.cw)))
        row = max(0, min(self.rows - 1, int(y // self.ch)))
        side_right = (x - col * self.cw) > (self.cw / 2)
        return col, row, side_right

    @staticmethod
    def _mods_bits(state):
        b = 0
        if state & Gdk.ModifierType.SHIFT_MASK:
            b |= 1
        if state & Gdk.ModifierType.ALT_MASK:
            b |= 2
        if state & Gdk.ModifierType.CONTROL_MASK:
            b |= 4
        return b

    def _app_mouse(self):
        if self.term is None:
            return False
        try:
            return (self.term.mouse_mode() & 0b111) != 0
        except Exception:
            return False

    def is_bracketed_paste(self):
        """True when the foreground app has enabled bracketed paste (?2004).

        Used to decide whether a paste should be wrapped in ESC[200~/ESC[201~.
        On an older core build without the `bracketed_paste` method we return
        True to preserve the previous (always-wrap) behavior — the fix only
        takes effect once the Rust core is rebuilt."""
        if self.term is None:
            return True
        fn = getattr(self.term, "bracketed_paste", None)
        if fn is None:
            return True
        try:
            return bool(fn())
        except Exception:
            return True

    def is_cursor_shown(self):
        """True unless the app hid the cursor with DECTCEM (ESC[?25l).

        Curses apps (mc, vim, less) hide the cursor while repainting and leave
        it parked in an arbitrary cell; drawing our block there paints a stray
        grey square over their output.  Falls back to True on an older core
        build without `cursor_visible` so behaviour is unchanged until the
        Rust core is rebuilt."""
        if self.term is None:
            return True
        fn = getattr(self.term, "cursor_visible", None)
        if fn is None:
            return True
        try:
            return bool(fn())
        except Exception:
            return True

    def _update_pointer_cursor(self):
        """Switch the pointer between I-beam and arrow based on context.

        I-beam ("text") whenever a selection is possible: normal shell mode,
        or any mode while Shift is held.  Arrow ("default") when a full-screen
        app is grabbing the mouse (mc, vim, …) and Shift isn't held."""
        if self.term is None:
            return
        want_text = (not self._app_mouse()) or self._shift_held
        if want_text == self._cur_cursor_is_text:
            return
        self._cur_cursor_is_text = want_text
        self.set_cursor(self._cursor_text if want_text else self._cursor_default)

    def _on_modifiers(self, _controller, state):
        self._shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)
        self._update_pointer_cursor()
        return False

    def _on_click_pressed(self, gesture, n_press, x, y):
        if self.term is None:
            return
        self.grab_focus()
        state = gesture.get_current_event_state()
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        btn = gesture.get_current_button()
        col, row, side = self._xy_to_cell(x, y)

        # Right-click → context menu (unless an app is grabbing the mouse and
        # Shift isn't held).  Do NOT clear the selection here, so the menu's
        # Copy can act on whatever is currently selected.
        if btn == 3 and not (self._app_mouse() and not shift):
            self.emit("context-menu", float(x), float(y))
            return

        # Shift+left-click extends the existing selection from its original
        # anchor to the clicked cell instead of starting a new one.  The core
        # stores anchors in absolute buffer coordinates, so this keeps working
        # after the view has been scrolled.  A non-extending click clears any
        # current selection first.
        extend_sel = (btn == 1 and shift and not self._app_mouse()
                      and self.get_has_selection())
        if not extend_sel:
            try:
                self.term.selection_clear()
            except Exception:
                pass
            self._sel_active = False
            self.queue_draw()

        if self._app_mouse() and not shift:
            rb = {1: 0, 2: 1, 3: 2}.get(btn)
            if rb is not None:
                self._mouse_report_btn = rb
                self._last_report_cell = (col, row)
                data = self.term.mouse_report(col, row, rb, 0, self._mods_bits(state))
                if data:
                    self.term.feed_input(bytes(data))
            return

        if btn == 1:
            if extend_sel:
                # Continue the existing selection to the clicked cell; keep it
                # active so a subsequent drag keeps extending.
                self.term.selection_update(col, row, side)
                self._sel_active = True
                self._copy_primary()
                self.queue_draw()
                return
            mode = "simple"
            if n_press == 2:
                mode = "word"
            elif n_press >= 3:
                mode = "line"
            self.term.selection_start(col, row, side, mode)
            self._sel_active = True
            if mode != "simple":
                self._copy_primary()
            self.queue_draw()
        elif btn == 2:
            self._paste_primary()

    def _on_click_released(self, gesture, n_press, x, y):
        if self.term is None:
            return
        state = gesture.get_current_event_state()
        btn = gesture.get_current_button()
        col, row, _side = self._xy_to_cell(x, y)

        if self._mouse_report_btn is not None:
            rb = {1: 0, 2: 1, 3: 2}.get(btn, self._mouse_report_btn)
            data = self.term.mouse_report(col, row, rb, 1, self._mods_bits(state))
            if data:
                self.term.feed_input(bytes(data))
            self._mouse_report_btn = None
            return

        if self._sel_active and btn == 1:
            self._sel_active = False
            self._copy_primary()

    def _on_motion(self, controller, x, y):
        if self.term is None:
            return
        col, row, side = self._xy_to_cell(x, y)
        self._ptr_cell = (col, row)
        state = controller.get_current_event_state()
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        self._shift_held = shift
        self._update_pointer_cursor()

        if self._sel_active:
            self.term.selection_update(col, row, side)
            self.queue_draw()
            return

        if self._mouse_report_btn is not None and not shift:
            if (col, row) == self._last_report_cell:
                return
            self._last_report_cell = (col, row)
            mm = self.term.mouse_mode()
            if mm & 0b110:
                data = self.term.mouse_report(
                    col, row, self._mouse_report_btn, 2, self._mods_bits(state))
                if data:
                    self.term.feed_input(bytes(data))

    # --- clipboard helpers (PRIMARY for selection; app drives CLIPBOARD) ---
    def _copy_primary(self):
        try:
            text = self.term.selection_text()
        except Exception:
            text = ""
        if text:
            self.get_primary_clipboard().set(text)

    def _paste_primary(self):
        clip = self.get_primary_clipboard()

        def _on_text(c, res):
            try:
                text = c.read_text_finish(res)
            except Exception:
                text = None
            if text and self.term is not None:
                try:
                    self.term.scroll_to_bottom()
                    self.term.feed_input(text.encode("utf-8", "replace"))
                except Exception:
                    pass

        clip.read_text_async(None, _on_text)

    # =======================================================================
    #  Keyboard
    # =======================================================================
    def _on_key(self, controller, keyval, keycode, state):
        if self.term is None:
            return False
        try:
            app_cursor = self.term.app_cursor()
        except Exception:
            app_cursor = False
        data = keyval_to_bytes(keyval, state, app_cursor, keycode)
        if data:
            try:
                self.term.selection_clear()
                self.term.scroll_to_bottom()
                self.term.feed_input(data)
            except Exception:
                pass
            self._blink_on = True
            self._last_blink = time.monotonic()
            return True
        return False

    # =======================================================================
    #  Per-frame tick: pull title / cursor / content changes -> signals.
    # =======================================================================
    def _tick(self, widget, frame_clock):
        if self.term is None:
            return GLib.SOURCE_CONTINUE
        try:
            title = self.term.title()
            exited = self.term.child_exited()
        except Exception:
            title, exited = "", False

        if exited and not self._exited:
            self._exited = True
            self.emit("child-exited", 0)
            return GLib.SOURCE_REMOVE

        # Cursor no longer blinks — it is drawn steadily (see _draw).

        if title != self._cur_title:
            self._cur_title = title
            self.emit("window-title-changed")

        # Background tabs keep ticking: GtkNotebook leaves non-current pages
        # rooted, so their frame-clock tick callback goes on firing at ~60 FPS.
        # Everything below is render bookkeeping — a full-grid snapshot() copy
        # (rows*cols*16 bytes), a byte compare, two more core lock round-trips —
        # and it is pure waste for a tab nobody is looking at.  With many tabs,
        # especially busy ssh sessions, that is the bulk of the idle cost.
        # Child-exit and title tracking above stay live for every tab.
        if not self.get_mapped():
            # Drop the cached frame so the first _draw after the tab is shown
            # again fetches fresh content instead of painting a stale grid.
            self._frame_snap = None
            return GLib.SOURCE_CONTINUE

        # Redraw only when something visible actually changed.  Unconditionally
        # queue_draw()-ing every frame forced a full Cairo repaint at ~60 FPS
        # even while idle, pegging one core.  All other visual-change sources
        # (selection, scrolling, search highlights, colour/config changes,
        # resize) already call queue_draw() themselves, so here we only need to
        # react to content changes (output) and cursor movement.
        dirty = False

        # Content change detection (one snapshot/frame, reused by _draw).
        try:
            snap = self.term.snapshot()
        except Exception:
            snap = None
        if snap is not None:
            if snap != self._frame_snap:
                self._frame_snap = snap
                dirty = True
                self.emit("contents-changed")
            else:
                self._frame_snap = snap

        # Include DECTCEM visibility in the compared state: an app toggling
        # ESC[?25l / ESC[?25h without moving the cursor must still repaint.
        try:
            cur = self.term.cursor() + (self.is_cursor_shown(),)
        except Exception:
            cur = None
        if cur is not None and cur != self._last_cursor:
            self._last_cursor = cur
            dirty = True
            self.emit("cursor-moved")

        # Follow dynamic mouse-mode changes (e.g. entering/leaving mc) so the
        # pointer cursor updates even without mouse movement.  Cheap: only
        # touches the cursor when the desired shape actually changes.
        self._update_pointer_cursor()

        self._refresh_adjustment()
        if dirty:
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    # =======================================================================
    #  Resize
    # =======================================================================
    def _reflow_to_size(self):
        alloc_w = self.get_width()
        alloc_h = self.get_height()
        if alloc_w <= 0 or alloc_h <= 0 or self.term is None:
            return
        self._on_resize(self, alloc_w, alloc_h)

    def _on_resize(self, area, width, height):
        if self.term is None:
            return
        new_cols = max(1, int(width // self.cw))
        new_rows = max(1, int(height // self.ch))
        if new_cols == self.cols and new_rows == self.rows:
            return
        try:
            self.term.resize(new_rows, new_cols)
        except Exception:
            return
        self.cols = new_cols
        self.rows = new_rows
        self._prev_snap = None
        self._frame_snap = None  # stale: was sized for the old grid
        self._refresh_adjustment()
        self.queue_draw()

    # =======================================================================
    #  Rendering: glyph cache, run-coalesced rows, persistent surface +
    #  per-row diff, overlays on top.
    # =======================================================================
    def _render_row(self, ctx, snap, r):
        cw, ch = self.cw, self.ch
        cols = self.cols
        y = r * ch
        base = r * cols
        unpack = CELL_STRUCT.unpack_from
        cells = [unpack(snap, (base + c) * 16) for c in range(cols)]

        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(self._def_bg[0], self._def_bg[1], self._def_bg[2],
                            self._def_bg_a)
        ctx.rectangle(0, y, cw * cols, ch)
        ctx.fill()
        ctx.set_operator(cairo.OPERATOR_OVER)

        # background runs
        c = 0
        while c < cols:
            cp, fg, bg, fl = cells[c]
            inv = bool(fl & F_INVERSE)
            bg_rgb = self._resolve_color(fg if inv else bg, is_fg=inv)
            if bg_rgb == self._def_bg and not inv:
                c += 1
                continue
            start = c
            while c < cols:
                cp2, fg2, bg2, fl2 = cells[c]
                inv2 = bool(fl2 & F_INVERSE)
                rgb2 = self._resolve_color(fg2 if inv2 else bg2, is_fg=inv2)
                if rgb2 != bg_rgb:
                    break
                c += 1
            ctx.set_source_rgb(*bg_rgb)
            ctx.rectangle(start * cw, y, (c - start) * cw, ch)
            ctx.fill()

        # foreground glyphs — one Pango alpha-mask blit per non-blank cell.
        c = 0
        while c < cols:
            cp, fg, bg, fl = cells[c]
            if fl & F_WIDE_SPACER:
                c += 1
                continue
            inv = bool(fl & F_INVERSE)
            fg_packed = self._bright_if_bold(bg if inv else fg, bool(fl & F_BOLD))
            fg_rgb = self._resolve_color(fg_packed, is_fg=not inv)
            if fl & F_DIM:
                fg_rgb = tuple(v * 0.6 for v in fg_rgb)
            x = c * cw

            if cp and cp != 0x20:
                if (cp in _BOX_SEGMENTS or cp in _BOX_QUADRANTS
                        or cp in _BOX_SHADES):
                    # Seamless box/block glyphs as crisp vectors.
                    self._draw_box_glyph(ctx, cp, x, y, cw, ch, fg_rgb)
                else:
                    mask, _span = self._glyph_mask(
                        cp, bool(fl & F_BOLD), bool(fl & F_ITALIC))
                    ctx.set_source_rgb(*fg_rgb)
                    ctx.mask_surface(mask, round(x), round(y))

            if fl & F_UNDERLINE:
                ctx.set_source_rgb(*fg_rgb)
                ctx.rectangle(x, y + self.baseline + 1, cw, 1)
                ctx.fill()
            if fl & F_STRIKE:
                ctx.set_source_rgb(*fg_rgb)
                ctx.rectangle(x, y + ch * 0.5, cw, 1)
                ctx.fill()
            c += 1

    def _draw_box_glyph(self, ctx, cp, x, y, cw, ch, rgb):
        """Draw a box-drawing/block-element codepoint as vector shapes so it
        fills the cell exactly (no inter-row/column gaps)."""
        # Shade blocks: translucent fill over the whole cell.
        shade = _BOX_SHADES.get(cp)
        if shade is not None:
            ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], shade)
            ctx.rectangle(x, y, cw, ch)
            ctx.fill()
            return

        # Quadrant-decomposable block elements (halves, full, quadrants).
        quad = _BOX_QUADRANTS.get(cp)
        if quad is not None:
            ctx.set_source_rgb(*rgb)
            tl, tr, bl, br = quad
            mx = round(x + cw / 2.0)
            my = round(y + ch / 2.0)
            x0, y0, x1, y1 = round(x), round(y), round(x + cw), round(y + ch)
            if tl:
                ctx.rectangle(x0, y0, mx - x0, my - y0); ctx.fill()
            if tr:
                ctx.rectangle(mx, y0, x1 - mx, my - y0); ctx.fill()
            if bl:
                ctx.rectangle(x0, my, mx - x0, y1 - my); ctx.fill()
            if br:
                ctx.rectangle(mx, my, x1 - mx, y1 - my); ctx.fill()
            return

        seg = _BOX_SEGMENTS.get(cp)
        if seg is None:
            return
        ctx.set_source_rgb(*rgb)
        up, dn, lf, rt = seg
        cx = round(x + cw / 2.0)
        cy = round(y + ch / 2.0)
        x0, y0, x1, y1 = round(x), round(y), round(x + cw), round(y + ch)
        lw = max(1, round(cw * 0.09))       # light stroke
        hw = max(2, round(cw * 0.18))       # heavy stroke
        sep = max(1, round(cw * 0.14))      # half-gap between double lines

        def vrect(yy0, yy1, weight):
            if weight == 3:
                ctx.rectangle(cx - sep - lw, yy0, lw, yy1 - yy0); ctx.fill()
                ctx.rectangle(cx + sep, yy0, lw, yy1 - yy0); ctx.fill()
            else:
                w = lw if weight == 1 else hw
                ctx.rectangle(cx - w // 2, yy0, w, yy1 - yy0); ctx.fill()

        def hrect(xx0, xx1, weight):
            if weight == 3:
                ctx.rectangle(xx0, cy - sep - lw, xx1 - xx0, lw); ctx.fill()
                ctx.rectangle(xx0, cy + sep, xx1 - xx0, lw); ctx.fill()
            else:
                w = lw if weight == 1 else hw
                ctx.rectangle(xx0, cy - w // 2, xx1 - xx0, w); ctx.fill()

        # Arms overlap the centre slightly so the four directions join cleanly.
        if up:
            vrect(y0, cy + lw, up)
        if dn:
            vrect(cy - lw, y1, dn)
        if lf:
            hrect(x0, cx + lw, lf)
        if rt:
            hrect(cx - lw, x1, rt)

    def _draw(self, area, cr, width, height):
        # Always paint the background first so the widget never flashes white
        # (before spawn, before the first snapshot, or for a frame mid-resize).
        # Use OPERATOR_SOURCE + the default-bg alpha so window transparency is
        # honoured even on these early-return frames.
        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(self._def_bg[0], self._def_bg[1], self._def_bg[2],
                           self._def_bg_a)
        cr.paint()
        cr.restore()
        if self.term is None:
            return
        cw, ch = self.cw, self.ch
        cols, rows = self.cols, self.rows

        # The snapshot must match the current grid size.  _frame_snap is fetched
        # asynchronously in _tick and can lag a resize by one frame, so validate
        # its length and re-fetch a fresh, correctly-sized snapshot if it does
        # not match — otherwise _render_row reads past the end of the buffer.
        expected = cols * rows * 16
        snap = self._frame_snap
        if snap is None or len(snap) != expected:
            try:
                snap = self.term.snapshot()
            except Exception:
                return
            if len(snap) != expected:
                return  # grid size in flux; skip this frame, bg already painted
        w_i, h_i = max(1, int(width)), max(1, int(height))
        dims = (w_i, h_i, cols, rows)

        if self._screen is None or self._screen_dims != dims:
            self._screen = cairo.ImageSurface(cairo.FORMAT_ARGB32, w_i, h_i)
            self._screen_dims = dims
            self._prev_snap = None

        sctx = cairo.Context(self._screen)
        row_bytes = cols * 16

        if self._prev_snap is None or len(self._prev_snap) != len(snap):
            for r in range(rows):
                self._render_row(sctx, snap, r)
        else:
            curv = memoryview(snap)
            prevv = memoryview(self._prev_snap)
            for r in range(rows):
                o = r * row_bytes
                if curv[o:o + row_bytes] != prevv[o:o + row_bytes]:
                    self._render_row(sctx, snap, r)
            curv.release()
            prevv.release()
        self._prev_snap = snap

        # Replace (not compose) so the offscreen's per-pixel alpha — including
        # the translucent default background — reaches the transparent window.
        # Clip to the actual grid rectangle: the widget can be a few pixels
        # wider/taller than cols*cw / rows*ch, and that remainder strip is never
        # drawn into _screen (it stays transparent).  Leaving it outside the
        # SOURCE blit preserves the translucent default-bg painted by the guard
        # above, so no fully-transparent grey bars appear at the right/bottom.
        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.rectangle(0, 0, cols * cw, rows * ch)
        cr.clip()
        cr.set_source_surface(self._screen, 0, 0)
        cr.paint()
        cr.restore()

        # --- overlays (never baked into the buffer) ---
        # selection highlight
        try:
            spans = self.term.selection_spans()
        except Exception:
            spans = []
        if spans:
            cr.set_source_rgba(0.30, 0.50, 0.85, 0.40)
            for (srow, cs, ce) in spans:
                cr.rectangle(cs * cw, srow * ch, (ce - cs + 1) * cw, ch)
                cr.fill()

        # Search highlights, pulled live from the core for THIS frame.
        # The core re-runs the regex over the visible rows and returns
        # viewport-relative coordinates, so the highlight is by construction in
        # sync with the text it covers.  The previous design cached absolute
        # row indices in Python; those shift by one every time a full scrollback
        # evicts its top line, which made highlights slide away from their text
        # during streaming output.  End column is INCLUSIVE here.
        try:
            spans = self.term.search_visible()
        except Exception:
            spans = ()
        for span in spans:
            vrow, cs, ce, focused = span
            if vrow < 0 or vrow >= rows:
                continue
            if focused:
                cr.set_source_rgba(1.0, 0.6, 0.0, 0.5)
            else:
                cr.set_source_rgba(1.0, 1.0, 0.0, 0.35)
            cr.rectangle(cs * cw, vrow * ch, (ce - cs + 1) * cw, ch)
            cr.fill()

        # cursor (block)
        try:
            crow, ccol, vis = self.term.cursor()
        except Exception:
            crow, ccol, vis = 0, 0, False
        if (vis and self.is_cursor_shown()
                and 0 <= crow < rows and 0 <= ccol < cols):
            # Steady (non-blinking) cursor: always drawn while visible.
            cr.set_source_rgba(self._cursor_bg[0], self._cursor_bg[1],
                               self._cursor_bg[2], 0.65)
            cr.rectangle(ccol * cw, crow * ch, cw, ch)
            cr.fill()

    # ----------------------------------------------------------- lifecycle
    def shutdown(self):
        """Stop the render/poll tick (called on tab cleanup)."""
        if self._tick_id is not None:
            try:
                self.remove_tick_callback(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
