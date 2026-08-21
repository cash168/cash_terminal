"""TerminalApp — Gtk.Application, window, tabs, key handling, main()."""
import os
import sys
import signal
import argparse
import importlib.resources
from gi.repository import Gtk, Gdk, GLib, Pango
from . import config
from .config import _load_yaml_config, _hex_to_rgba
from .util import _translate_keyval_to_latin
from .tab import TerminalTab


# Reverse-DNS application identifier.  One constant because the same string has
# to appear in four places that the desktop environment cross-references: the
# GApplication ID (D-Bus single-instance name), the .desktop file name, its
# StartupWMClass key, and the X11 WM_CLASS property.  If any of them drifts,
# Cinnamon/Muffin stops matching the window to the launcher and grows a
# duplicate generic entry in the panel.
APP_ID = "org.cashterminal.CashTerminal"

def _remove_stale_desktop_entries(apps_dir, keep):
    """Delete .desktop files this app installed under a previous APP_ID.

    The app writes its own launcher, so renaming APP_ID orphans the old file
    and the desktop menu ends up with two identical entries — nothing else
    ever cleans up a file we wrote into ~/.local/share/applications.

    Identification is by content rather than by a list of past identifiers:
    that keeps retired names out of the source, and the next rename needs no
    code change here.

    Both conditions together are what make deleting safe.  `Exec` must be
    exactly our launcher, and `StartupWMClass` must equal the file's own
    stem — which is the shape the writer below produces, and which a launcher
    someone wrote by hand for cash-terminal would not have.
    """
    try:
        names = os.listdir(apps_dir)
    except OSError:
        return
    for name in names:
        if not name.endswith(".desktop") or name == keep:
            continue
        path = os.path.join(apps_dir, name)
        try:
            with open(path, "r") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        stem = name[:-len(".desktop")]
        if "Exec=cash-terminal" in lines and f"StartupWMClass={stem}" in lines:
            try:
                os.remove(path)
            except OSError:
                pass


class TerminalApp(Gtk.Application):
    """GTK4 terminal application with notebook tabs and single-instance D-Bus."""

    def __init__(self):
        from gi.repository import Gio
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._win = None
        self._notebook = None
        self._tab_counter = 0
        self._mru_history = []
        self._mru_frozen = False
        self._mru_cycle_index = 0
        self._ctrl_tab_armed = True
        self._seq_switching = False       # Ctrl+Left/Right deferred mode
        self._seq_origin_page = -1        # page before Ctrl+Left/Right sequence
        self._last_cwd = None
        # Tab list overlay state
        self._tab_list_active = False
        self._tab_list_selected = 0
        self._tab_list_origin_page = 0
        self._ctrl_tab_overlay_timer_id = 0
        # Remembered tab title
        self._remembered_tab_title = None
        self._remembered_win_title = None
        # Tab list overlay widgets
        self._tab_list_overlay_widget = None
        self._tab_list_overlay_parent = None
        self._tab_list_buttons = []
        # Close-confirmation via bottom bar (not dialog)
        self._close_confirm_pending = False

    def do_startup(self):
        """Called once on first activation — install icons/desktop BEFORE window.

        Cinnamon/Muffin matches windows to .desktop files by StartupWMClass.
        If .desktop doesn't exist when the window appears, Cinnamon creates
        a generic panel entry → then a second one when .desktop appears later.
        Installing here ensures .desktop + PNGs exist before any window.
        """
        Gtk.Application.do_startup(self)
        self._svg_data = self._install_icons()
        self._install_fallback_icons()

    def do_command_line(self, command_line):
        """Handle command line — D-Bus single instance support.

        When a second instance is launched, GApplication sends the command
        line to the running instance via D-Bus.  We open a new tab.
        """
        self.activate()
        args = command_line.get_arguments()
        # Parse --directory from args
        start_dir = None
        for i, arg in enumerate(args):
            if arg in ("--directory", "-d") and i + 1 < len(args):
                d = os.path.expanduser(args[i + 1])
                if os.path.isdir(d):
                    start_dir = d
                break
            if arg.startswith("--directory="):
                d = os.path.expanduser(arg.split("=", 1)[1])
                if os.path.isdir(d):
                    start_dir = d
                break
        # If this is a second invocation (window already exists), open new tab
        if self._win is not None and self._notebook.get_n_pages() > 0:
            # Only add tab if this is truly a second invocation
            pass  # activate() already handles it
        return 0

    def do_activate(self):
        """Create window and first tab."""
        if self._win is not None:
            # Second instance → new tab, in the same place any other new tab
            # would open (startup.directory, or the current one when
            # startup.follow_cwd is on).
            self._add_tab()
            self._win.present()
            return

        win = Gtk.ApplicationWindow(application=self)
        win.set_title("Cash Terminal")

        # Icons were installed in do_startup() before window creation
        _svg_data = getattr(self, '_svg_data', None)
        win.set_icon_name("cash-terminal")

        # Connect to 'realize' signal to set WM_CLASS BEFORE the window
        # is mapped.  At realize time the X window (surface) exists but is
        # not yet visible — so Cinnamon/Muffin will see the correct
        # WM_CLASS from the very first MapNotify and match it to .desktop.
        def _on_realize(w):
            self._set_x11_properties(w, _svg_data)
        win.connect("realize", _on_realize)

        if config.STARTUP_WINDOW_SIZE:
            win.set_default_size(*config.STARTUP_WINDOW_SIZE)
        win.set_size_request(640, 480)
        if config.STARTUP_MAXIMIZED:
            win.maximize()
        win.add_css_class("cash-terminal-window")
        self._win = win

        notebook = Gtk.Notebook()
        notebook.add_css_class("cash-terminal-notebook")
        notebook.set_scrollable(True)
        notebook.set_show_border(False)
        notebook.set_vexpand(True)
        notebook.set_hexpand(True)
        notebook.connect("switch-page", self._on_switch_page)
        self._notebook = notebook
        win.set_child(notebook)

        # Left-side action widget: [◀] prev tab
        prev_btn = Gtk.Button(label="\u25C0")
        prev_btn.set_has_frame(False)
        prev_btn.add_css_class("tab-action-btn")
        prev_btn.add_css_class("tab-nav-btn")
        prev_btn.set_focusable(False)
        prev_btn.set_can_focus(False)
        prev_btn.set_tooltip_text("Previous tab")
        prev_btn.connect("clicked", self._on_prev_tab_clicked)
        prev_btn.set_visible(False)  # hidden until tabs overflow
        self._prev_btn = prev_btn
        notebook.set_action_widget(prev_btn, Gtk.PackType.START)

        # Right-side action buttons: [▶] next tab, [+] new tab, [▼] tabs list,
        # [⚙] settings
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        next_btn = Gtk.Button(label="\u25B6")
        next_btn.set_has_frame(False)
        next_btn.add_css_class("tab-action-btn")
        next_btn.add_css_class("tab-nav-btn")
        next_btn.set_focusable(False)
        next_btn.set_can_focus(False)
        next_btn.set_tooltip_text("Next tab")
        next_btn.connect("clicked", self._on_next_tab_clicked)
        next_btn.set_visible(False)  # hidden until tabs overflow
        self._next_btn = next_btn
        action_box.append(next_btn)

        add_btn = Gtk.Button(label="+")
        add_btn.set_has_frame(False)
        add_btn.add_css_class("tab-action-btn")
        add_btn.add_css_class("tab-add-btn")
        add_btn.set_tooltip_text("New tab (Ctrl+T)")
        add_btn.connect("clicked", lambda b: self._add_tab())
        action_box.append(add_btn)

        list_btn = Gtk.Button(label="\u25bc")
        list_btn.set_has_frame(False)
        list_btn.add_css_class("tab-action-btn")
        list_btn.set_tooltip_text("Tab list (Ctrl+Up/Down)")
        list_btn.set_focusable(False)
        list_btn.set_can_focus(False)
        list_btn.connect("clicked", lambda b: self._toggle_tab_list())
        self._tab_list_btn = list_btn
        action_box.append(list_btn)

        # U+FE0E (text presentation selector) keeps the gear a glyph in the UI
        # font; without it some emoji fonts substitute a full-colour picture
        # that sits badly next to the flat ▶ + ▼ next to it.
        gear_btn = Gtk.Button(label="⚙︎")
        gear_btn.set_has_frame(False)
        gear_btn.add_css_class("tab-action-btn")
        gear_btn.add_css_class("tab-gear-btn")
        gear_btn.set_tooltip_text("Настройки терминала")
        gear_btn.set_focusable(False)
        gear_btn.set_can_focus(False)
        gear_btn.connect("clicked", lambda b: self.open_settings())
        action_box.append(gear_btn)

        notebook.set_action_widget(action_box, Gtk.PackType.END)

        # CSS
        css = Gtk.CssProvider()
        css.load_from_string(f"""
            window.cash-terminal-window {{
                background-color: rgba(0, 0, 0, 0);
            }}

            notebook.cash-terminal-notebook,
            notebook.cash-terminal-notebook > stack {{
                background-color: transparent;
            }}

            notebook.cash-terminal-notebook > header.top {{
                background: #d8d8d8;
                border: none;
                border-bottom: 1px solid #AFAFAF;
                padding: 0;
                min-height: 28px;
            }}

            notebook.cash-terminal-notebook > header.top > tabs > tab {{
                min-height: 28px;
                padding: 0 4px;
                margin: 0;
                background: linear-gradient(to bottom, {config.TAB_THEME["inactive_gradient_top"]}, {config.TAB_THEME["inactive_gradient_bottom"]});
                color: #2a2a2a;
                border: 1px solid #AFAFAF;
                border-bottom: none;
                border-radius: 3px 3px 0 0;
                transition: background {config.TAB_SWITCH_ANIMATION_MS}ms ease;
            }}

            notebook.cash-terminal-notebook > header.top > tabs > tab:checked {{
                background: linear-gradient(to bottom, {config.TAB_THEME["active_gradient_top"]}, {config.TAB_THEME["active_gradient_bottom"]});
                color: #202020;
                box-shadow: inset 0 2px 0 {config.TAB_THEME["active_bar_color"]};
            }}

            notebook.cash-terminal-notebook > header.top > tabs > tab:hover {{
                background: linear-gradient(to bottom, {config.TAB_THEME["hover_gradient_top"]}, {config.TAB_THEME["hover_gradient_bottom"]});
            }}

            .tab-action-btn {{
                min-width: 24px;
                min-height: 24px;
                padding: 0;
                margin: 2px;
                background: none;
                border: 1px solid transparent;
                border-radius: 3px;
                color: #505050;
            }}

            .tab-action-btn:hover {{
                background: rgba(0, 0, 0, 0.08);
                border-color: #AFAFAF;
            }}

            .tab-add-btn {{
                font-size: 16px;
                font-weight: bold;
            }}

            .tab-gear-btn {{
                font-size: 14px;
            }}

            .tab-close {{
                min-width: 18px;
                min-height: 18px;
                padding: 0;
                margin: 0 4px 0 4px;
                background: none;
                color: #404040;
            }}

            .tab-label {{
                color: #2a2a2a;
            }}

            .tab-nav-btn {{
                font-size: 10px;
                min-width: 20px;
                min-height: 20px;
                padding: 0 2px;
            }}

            .tab-nav-btn:disabled {{
                color: #c0c0c0;
            }}

            /* Hide built-in scroll arrows — we use our own ◀ ▶ buttons */
            notebook.cash-terminal-notebook > header.top > tabs > arrow {{
                min-width: 0;
                min-height: 0;
                padding: 0;
                margin: 0;
                border: none;
                opacity: 0;
            }}

            notebook.cash-terminal-notebook > header.top > tabs > tab:focus,
            notebook.cash-terminal-notebook > header.top > tabs > tab:focus-visible {{
                border: none;
                outline: none;
                box-shadow: none;
            }}

            .tab-list-overlay {{
                background: rgba(46, 50, 56, 0.97);
                border-radius: 4px;
                border: 1px solid rgba(100, 110, 120, 0.6);
                padding: 4px 0;
                min-width: 300px;
            }}

            .tab-list-item {{
                padding: 4px 14px;
                min-height: 26px;
                border-radius: 0;
                color: #c0c0c0;
                font-size: 13px;
            }}

            .tab-list-item:hover {{
                background: rgba(255, 255, 255, 0.08);
            }}

            .tab-list-item-active {{
                color: #e0e0e0;
                border-left: 3px solid #6E9CBB;
                padding-left: 11px;
            }}

            .tab-list-item-selected {{
                background: rgba(60, 90, 130, 0.7);
                color: #ffffff;
            }}

            vte-terminal {{
                background-color: rgba({int(config.BG_COLOR[1:3], 16)}, {int(config.BG_COLOR[3:5], 16)}, {int(config.BG_COLOR[5:7], 16)}, {config.BG_ALPHA});
                color: {config.FG_COLOR};
            }}

            .search-bar {{
                background: rgba(46, 50, 56, 0.97);
                border: 1px solid rgba(100, 110, 120, 0.6);
                border-radius: 4px;
                padding: 4px 8px;
            }}

            .search-entry {{
                min-height: 26px;
                min-width: 200px;
                background: rgba(30, 32, 36, 0.95);
                color: #e0e0e0;
                caret-color: #e0e0e0;
                border: 1px solid rgba(100, 110, 120, 0.6);
                border-radius: 3px;
                padding: 1px 6px;
                font-size: 13px;
            }}

            .search-entry.search-match {{
                border-color: rgba(80, 200, 80, 0.8);
            }}

            .search-entry.search-no-match {{
                border-color: rgba(220, 60, 60, 0.8);
            }}

            .search-status {{
                color: #c0c0c0;
                font-size: 11px;
                min-width: 60px;
            }}

            .search-nav-btn {{
                min-width: 26px;
                min-height: 26px;
                padding: 0;
                color: #c0c0c0;
                background: none;
                border: none;
                font-size: 11px;
            }}

            .search-nav-btn:hover {{
                background: rgba(255, 255, 255, 0.08);
                border-radius: 3px;
            }}

            .search-close-btn {{
                min-width: 26px;
                min-height: 26px;
                padding: 0;
                color: #c0c0c0;
                background: none;
                border: none;
            }}

            .search-close-btn:hover {{
                background: rgba(255, 255, 255, 0.08);
                border-radius: 3px;
            }}

            .confirm-bar {{
                background: rgba(180, 80, 20, 0.92);
                color: #ffffff;
                padding: 6px 12px;
                font-family: {config.FONT_FAMILY};
                font-size: {config.FONT_SIZE_PT}pt;
                font-weight: bold;
                /* margin-end set dynamically by _sync_overlay_margins() */
            }}

            .session-picker {{
                background: rgba(38, 42, 48, 0.98);
                border: 1px solid rgba(100, 110, 120, 0.6);
                border-radius: 6px;
                padding: 12px 14px;
                min-width: 420px;
            }}

            .session-picker-title {{
                color: #e0e0e0;
                font-weight: bold;
                font-size: 13px;
                margin-bottom: 8px;
            }}

            .session-picker-list {{
                background: transparent;
            }}

            .session-picker-list row {{
                padding: 5px 10px;
                border-radius: 4px;
                color: #d0d0d0;
            }}

            /* Hover must be spelled out: the stock theme paints its own light
               prelight here, which on a dark popup gives white-on-pale-grey. */
            .session-picker-list row:hover {{
                background: rgba(60, 90, 130, 0.45);
                color: #ffffff;
            }}

            .session-picker-list row:selected,
            .session-picker-list row:selected:hover {{
                background: rgba(60, 90, 130, 0.85);
                color: #ffffff;
            }}

            .session-picker-list row:hover .session-picker-sub {{
                color: #d8e2ec;
            }}

            /* The stock theme paints its own (light) hover background here;
               on the picker's dark panel that leaves pale grey text on white.
               Override hover explicitly — including over a selected row, so
               the pointer never washes out the current choice. */
            .session-picker-list row:hover {{
                background: rgba(255, 255, 255, 0.10);
                color: #ffffff;
            }}

            .session-picker-list row:selected:hover {{
                background: rgba(72, 106, 150, 0.95);
                color: #ffffff;
            }}

            .session-picker-list row:hover .session-picker-sub {{
                color: #d8e2ec;
            }}

            /* Section header — a widget attached above a row via
               set_header_func, not a row itself (see session_picker.py).
               The rule under the text is what actually makes a group read as
               a group; without it the title just floats above the rows. */
            .session-picker-header {{
                color: #8a929c;
                font-size: 11px;
                padding: 12px 10px 3px 10px;
                margin-bottom: 4px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.18);
            }}

            .session-picker-list separator {{
                background: rgba(255, 255, 255, 0.12);
                margin: 8px 6px 4px 6px;
            }}

            .session-picker-name {{
                font-size: 13px;
            }}

            .session-picker-sub {{
                color: #9aa3ad;
                font-size: 11px;
                font-family: {config.FONT_FAMILY};
            }}

            .session-picker-list row:selected .session-picker-sub {{
                color: #d8e2ec;
            }}

            .session-picker-hint {{
                color: #8a929c;
                font-size: 11px;
                margin-top: 10px;
            }}

            /* Favourites editor: drag handle and drop indicator.  The
               indicator is a border on the row the insertion lands next to,
               not a placeholder widget inserted into the list — a placeholder
               would shift the very heights the drop position is measured
               from, so the target would move as the pointer approached it. */
            .connections-drag-handle {{
                padding: 0 6px;
                font-size: 15px;
            }}

            .connections-dragging {{
                opacity: 0.4;
            }}

            .connections-drop-above {{
                border-top: 2px solid #4a90d9;
            }}

            .connections-drop-below {{
                border-bottom: 2px solid #4a90d9;
            }}

            .context-menu {{
                padding: 4px;
                min-width: 180px;
            }}

            .context-menu-item {{
                padding: 5px 12px;
                border-radius: 4px;
            }}
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # GTK4 notebook header steals focus on click — refocus terminal
        def _on_nb_click_released(gesture, n_press, x, y):
            self._refocus_active_terminal_delayed()
        _nb_click = Gtk.GestureClick()
        _nb_click.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        _nb_click.connect("released", _on_nb_click_released)
        notebook.add_controller(_nb_click)

        # Window-level key handler for tab shortcuts
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        key_ctrl.connect("key-released", self._on_key_released)
        win.add_controller(key_ctrl)

        # First tab
        self._add_tab()

        # Update ◀▶ visibility when window is resized
        win.connect("notify::default-width", lambda *a: self._update_nav_buttons_visibility())

        win.connect("close-request", self._on_close)
        win.present()

        # WM_CLASS + _NET_WM_ICON are set in the 'realize' handler above,
        # which fires before the window is mapped — no timeout needed.

    def _inherited_start_dir(self):
        """Working directory a new tab should inherit, or None.

        None means "no opinion" — the tab then falls back to
        config.STARTUP_DIRECTORY, which is exactly the behaviour there has
        always been, so the setting being off costs nothing.
        """
        if not config.STARTUP_FOLLOW_CWD:
            return None
        tab = self._get_visible_terminal()
        if tab is None:
            return None  # the very first tab: there is nothing to inherit from
        try:
            return tab.get_cwd()
        except Exception:
            return None  # /proc is a convenience here, never worth a failed tab

    def _add_tab(self, start_dir=None, show_picker=None):
        """Create a new terminal tab.

        `show_picker` None = follow the `new_tab.action` config option; pass an
        explicit bool to force one behaviour for a particular caller.

        `start_dir` None = decide here.  An explicit directory (--directory on
        the command line) always wins over startup.follow_cwd: it was asked for
        by name.
        """
        self._tab_counter += 1
        if start_dir is None:
            start_dir = self._inherited_start_dir()
        if show_picker is None:
            show_picker = (config.NEW_TAB_ACTION == "picker")
        tab = TerminalTab(start_dir=start_dir, show_picker=show_picker)

        # Tab label with close button
        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tab_box.set_hexpand(True)
        tab_box.set_margin_start(10)
        tab_box.set_margin_end(6)

        # Use remembered tab title so new tabs show a realistic label
        # immediately (e.g. "~ : bash") instead of generic "Terminal N".
        # A picker tab is different: it has no child process, so nothing would
        # ever overwrite that placeholder — it would sit there showing the
        # title of some long-closed tab until a session is actually chosen.
        if show_picker:
            _tab_title = TerminalTab.PICKER_TITLE
        else:
            _tab_title = self._remembered_tab_title or f"Terminal {self._tab_counter}"
        # Pre-set _last_tab_title so _refresh_tab_title() sees it as current
        # and doesn't overwrite with garbage before shell is spawned.
        tab._last_tab_title = _tab_title

        label = Gtk.Label(label=_tab_title)
        label.add_css_class("tab-label")
        label.set_hexpand(True)
        label.set_xalign(0.5)
        label.set_width_chars(12)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        tab.bind_tab_label(label)
        tab_box.append(label)

        close_btn = Gtk.Button()
        close_btn.set_icon_name("window-close-symbolic")
        close_btn.set_has_frame(False)
        close_btn.add_css_class("tab-close")
        close_btn.connect("clicked", lambda b, t=tab: self.close_tab(t))
        tab_box.append(close_btn)

        self._notebook.append_page(tab, tab_box)
        page = self._notebook.get_page(tab)
        if page:
            page.set_property("tab-expand", True)
            page.set_property("reorderable", True)

        self._notebook.set_current_page(self._notebook.page_num(tab))
        self._remember_tab(tab)
        tab.grab_terminal_focus()
        self._update_nav_buttons_visibility()

    def _update_remembered_title(self, tab=None):
        """Snapshot tab title from the given (or current) tab.

        Called when closing a tab so that _remembered_tab_title stays
        up-to-date for new tabs.
        """
        if tab is None:
            tab = self._get_visible_terminal()
        if tab is None:
            return
        if getattr(tab, "_child_pid", -1) <= 0:
            return  # picker tab: "Выбор сессии" is not a useful seed title
        title = tab._last_tab_title
        if title:
            self._remembered_tab_title = title
            self._remembered_win_title = f"Cash Terminal \u2014 {title}"

    def close_tab(self, tab):
        """Close a terminal tab — always switch to adjacent (right, then left).

        Opera-like behavior: both Ctrl+W and clicking × use the same
        adjacent-tab strategy so the × button stays under the cursor
        for rapid sequential closes.  MRU history is still maintained
        (the closed tab is removed from it) so Ctrl+Tab keeps working.
        """
        page_num = self._notebook.page_num(tab)
        if page_num < 0:
            return

        # Snapshot title from the tab being closed so that
        # _remembered_tab_title stays up-to-date for new tabs.
        self._update_remembered_title(tab)

        is_current = (self._get_visible_terminal() is tab)
        n_pages = self._notebook.get_n_pages()

        # --- Pick adjacent tab: prefer right, then left ---
        target = None
        if is_current and n_pages > 1:
            if page_num < n_pages - 1:
                target = self._notebook.get_nth_page(page_num + 1)
            elif page_num > 0:
                target = self._notebook.get_nth_page(page_num - 1)

        tab.cleanup()
        if tab in self._mru_history:
            self._mru_history.remove(tab)

        self._notebook.remove_page(page_num)
        self._update_nav_buttons_visibility()

        if self._notebook.get_n_pages() == 0:
            self.quit()
            return

        if target is not None and self._notebook.page_num(target) >= 0:
            self._notebook.set_current_page(self._notebook.page_num(target))

        # Ensure the now-visible tab has keyboard focus
        visible = self._get_visible_terminal()
        if visible is not None:
            GLib.idle_add(visible.grab_terminal_focus)

    def _on_switch_page(self, notebook, page, page_num):
        """Focus terminal when switching tabs."""
        if not self._mru_frozen:
            self._remember_tab(page)
        # Keep remembered title up-to-date for new tabs
        self._update_remembered_title(page)
        # Use page_num (the NEW page) for sensitivity — get_current_page()
        # still returns the OLD page during switch-page signal.
        n = notebook.get_n_pages()
        if hasattr(self, '_prev_btn'):
            self._prev_btn.set_sensitive(page_num > 0)
        if hasattr(self, '_next_btn'):
            self._next_btn.set_sensitive(page_num < n - 1)
        # Use idle_add to ensure focus is set after GTK finishes page switch
        GLib.idle_add(page.grab_terminal_focus)
        # Push the newly active tab's title to the window.  Deferred to idle
        # because during switch-page get_current_page() still returns the OLD
        # page, and the tab's own refresh timer skips background tabs.
        if hasattr(page, "sync_window_title"):
            GLib.idle_add(page.sync_window_title)

    def _remember_tab(self, tab):
        """Push tab to front of MRU history."""
        if tab in self._mru_history:
            self._mru_history.remove(tab)
        self._mru_history.insert(0, tab)

    def _get_visible_terminal(self):
        """Return the currently visible TerminalTab or None."""
        cur = self._notebook.get_current_page()
        if cur >= 0:
            return self._notebook.get_nth_page(cur)
        return None

    def apply_appearance_to_all_tabs(self):
        """Re-apply current config colours + font to every open tab.

        Used by the Settings dialog for live preview (and Save/Cancel)."""
        if self._notebook is None:
            return
        for i in range(self._notebook.get_n_pages()):
            tab = self._notebook.get_nth_page(i)
            if tab is None:
                continue
            try:
                tab._apply_colors()
            except Exception:
                pass
            try:
                tab.apply_font()
            except Exception:
                pass

    def open_settings(self):
        """Open the appearance Settings dialog (single instance)."""
        existing = getattr(self, "_settings_dialog", None)
        if existing is not None:
            existing.present()
            return
        from .settings import SettingsDialog
        dialog = SettingsDialog(self._win, self.apply_appearance_to_all_tabs,
                                self._on_settings_closed)
        self._settings_dialog = dialog
        dialog.present()

    def open_connections(self):
        """Open the favourites/history editor (single instance)."""
        existing = getattr(self, "_connections_dialog", None)
        if existing is not None:
            existing.present()
            return
        from .connections_dialog import ConnectionsDialog
        dialog = ConnectionsDialog(self._win, self._on_connections_closed)
        self._connections_dialog = dialog
        dialog.present()

    def _on_connections_closed(self):
        """Editor closed — refresh every open picker and drop the reference."""
        self._connections_dialog = None
        for i in range(self._notebook.get_n_pages()):
            page = self._notebook.get_nth_page(i)
            if hasattr(page, "reload_picker"):
                page.reload_picker()
        visible = self._get_visible_terminal()
        if visible is not None:
            visible.grab_terminal_focus()

    def _on_settings_closed(self):
        """Called by the Settings dialog when it closes — drop the reference."""
        self._settings_dialog = None
        visible = self._get_visible_terminal()
        if visible is not None:
            visible.grab_terminal_focus()

    def _on_key_pressed(self, controller, keyval, keycode, state):
        """Window-level shortcuts with non-Latin keyboard layout support."""
        mods = state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(mods & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(mods & Gdk.ModifierType.ALT_MASK)
        
        # Translate to Latin keyval for shortcuts (handles non-Latin layouts)
        latin_kv = _translate_keyval_to_latin(keycode, keyval)
        
        # Get current terminal tab
        tab = self._get_visible_terminal()

        # --- Close-confirmation bar (app-level, highest priority) ---
        if self._close_confirm_pending:
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._close_confirm_pending = False
                if tab:
                    tab._confirm_bar.set_visible(False)
                self._do_close()
                return True
            if keyval == Gdk.KEY_Escape:
                self._close_confirm_pending = False
                if tab:
                    tab._confirm_bar.set_visible(False)
                return True
            # Block all other keys while close-confirm is active
            return True

        # --- Confirmation bar handling (per-tab, paste/command) ---
        if tab and tab._confirm_pending:
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                tab._confirm_accept()
                return True
            if keyval == Gdk.KEY_Escape:
                tab._confirm_cancel()
                return True
            # Block all other keys while confirm is active
            return True

        # --- Tab list overlay navigation ---
        if self._tab_list_active:
            n = self._notebook.get_n_pages()
            if keyval == Gdk.KEY_Escape:
                self._close_tab_list(activate=False)
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._close_tab_list(activate=True)
                return True
            # Up/Down navigate (with or without Ctrl held)
            if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
                self._tab_list_selected = (self._tab_list_selected - 1) % n
                self._update_tab_list_selection()
                return True
            if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
                self._tab_list_selected = (self._tab_list_selected + 1) % n
                self._update_tab_list_selection()
                return True
            # Ctrl+Left/Right: navigate selection inside overlay (keep it open)
            if ctrl and keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
                if keyval == Gdk.KEY_Left:
                    new_sel = self._tab_list_selected - 1
                    if new_sel < 0:
                        new_sel = 0
                else:
                    new_sel = self._tab_list_selected + 1
                    if new_sel >= n:
                        new_sel = n - 1
                self._tab_list_selected = new_sel
                self._update_tab_list_selection()
                # Also switch the actual tab so preview matches
                self._notebook.set_current_page(new_sel)
                # Ensure sequential switching state is active
                if not self._seq_switching:
                    self._seq_switching = True
                    self._seq_origin_page = self._tab_list_origin_page
                    self._mru_frozen = True
                return True
            # Ctrl+Tab/Shift+Tab: let it fall through to MRU handler (#8)
            elif ctrl and keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
                pass  # fall through to Ctrl+Tab handler below
            # Ctrl+T / Ctrl+W: let fall through to create/close tab with overlay open
            elif ctrl and _translate_keyval_to_latin(keycode, keyval) in (
                    Gdk.KEY_t, Gdk.KEY_T, Gdk.KEY_w, Gdk.KEY_W):
                pass  # fall through to Ctrl+T / Ctrl+W handlers below
            else:
                # Any other key closes tab list and activates selection
                self._close_tab_list(activate=True)
                # Don't return True — let the key propagate (e.g. typing)
                return False

        # --- Session picker owns the keyboard while it is up ---
        # Handled here rather than left to the ListBox: this controller is on
        # the window in the CAPTURE phase, so it sees the key whatever holds
        # the focus.  A click next to the picker (it is an overlay — the click
        # lands on the terminal underneath) used to move the focus away and
        # kill the arrow keys for good; now focus does not matter, and the
        # picker pulls it back on the first arrow press.  Ctrl/Alt shortcuts
        # still work, so a tab without a shell is never a dead end.
        if tab is not None and getattr(tab, "_picker", None) is not None:
            if not ctrl and not alt:
                return tab._picker.handle_key(keyval)

        # --- Search bar keys: ONLY when the search entry has focus ---
        # When focus is on the terminal (e.g. a shell, or a TUI app like mc /
        # mcedit), Escape / Ctrl+C / Enter / F3 must reach the terminal — they
        # must NOT close the search bar or drive search navigation.  This keeps
        # the terminal fully usable while the (non-modal) search bar is open.
        if tab and tab._search_active:
            entry_focused = bool(tab._search_entry.get_state_flags() & Gtk.StateFlags.FOCUS_WITHIN)
            if entry_focused:
                # Escape / Ctrl+C close the search bar.
                is_esc = (keyval == Gdk.KEY_Escape)
                is_ctrl_c = (ctrl and not shift and latin_kv in (Gdk.KEY_c, Gdk.KEY_C))
                if is_esc or is_ctrl_c:
                    tab._close_search()
                    return True
                # Enter (no shift): search UP (bottom→up, toward older output).
                # Shift+Enter: search DOWN.  forward=False == up, so forward=shift.
                if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                    if tab._search_entry.get_text():
                        tab._search_find(forward=shift)
                    return True
                if keyval == Gdk.KEY_F3:
                    if tab._search_entry.get_text():
                        tab._search_find(forward=shift)
                    return True
                # Paste INTO the search entry.  The window handler below blocks
                # Ctrl+V and routes Ctrl+Insert/Ctrl+Shift+V to the terminal, so
                # without this the search field can never be pasted into.
                is_paste = (
                    (ctrl and latin_kv in (Gdk.KEY_v, Gdk.KEY_V)) or
                    (ctrl and keyval == Gdk.KEY_Insert) or
                    (shift and not ctrl and keyval == Gdk.KEY_Insert)
                )
                if is_paste:
                    tab._paste_into_search_entry()
                    return True

        # --- Shift+Insert: paste with security ---
        if shift and not ctrl and keyval == Gdk.KEY_Insert:
            if tab:
                tab._paste_from_clipboard()
                return True

        # --- Ctrl+V (no Shift): completely block VTE's native paste ---
        # VTE binds Ctrl+V to paste-clipboard internally.  We intercept it
        # in CAPTURE phase and swallow the key — paste is only allowed via
        # secure handlers: Shift+Insert or Ctrl+Shift+V.
        if ctrl and not shift:
            if latin_kv in (Gdk.KEY_v, Gdk.KEY_V):
                return True  # block — do nothing

        # --- Ctrl+Shift+V: paste with security ---
        if ctrl and shift:
            if latin_kv in (Gdk.KEY_v, Gdk.KEY_V):
                if tab:
                    tab._paste_from_clipboard()
                    return True

        # --- Ctrl+Shift+C: copy ---
        if ctrl and shift:
            if latin_kv in (Gdk.KEY_c, Gdk.KEY_C):
                if tab:
                    tab.terminal.copy_clipboard_format(None)
                    return True

        # --- Ctrl+Insert: copy ---
        if ctrl and not shift and keyval == Gdk.KEY_Insert:
            if tab:
                tab.terminal.copy_clipboard_format(None)
                return True

        if not ctrl:
            # --- PageUp/PageDown: scroll viewport in normal mode only ---
            # In alt screen (mc, vim, htop) these keys are sent to the app.
            # In normal mode, scroll the terminal's scrollback buffer.
            # We also intercept during active output (e.g. `find /`) even
            # if _is_alt_screen() returns True due to foreground process
            # detection — the user wants to scroll, not send ^[[5~ to PTY.
            if tab and keyval in (Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up,
                                  Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down,
                                  Gdk.KEY_KP_9, Gdk.KEY_KP_3,
                                  0xFF55, 0xFF56):
                # Use vadjustment to determine if scrollback exists.
                # If upper > page_size, there's scrollback — we're in
                # normal mode (not alt screen) and should scroll.
                # Alt screen apps (mc, vim) have upper == page_size.
                vadj = tab.terminal.get_vadjustment()
                has_scrollback = False
                if vadj:
                    has_scrollback = vadj.get_upper() > vadj.get_page_size() + 0.5
                is_alt = tab._is_alt_screen()
                # When SSH is active, TIOCGPGRP sees "ssh" as foreground —
                # _is_alt_screen() returns False even though the remote app
                # (e.g. mcedit) is in alt screen.  In that case we must NOT
                # intercept PageUp/PageDown — pass them through to the PTY
                # so the remote TUI app receives \x1b[5~ / \x1b[6~.
                # We only scroll locally when there is actual scrollback content.
                if has_scrollback:
                    # Scrollback exists — scroll locally, don't send to PTY
                    if vadj:
                        page = vadj.get_page_size()
                        if keyval in (Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
                            vadj.set_value(max(vadj.get_value() - page,
                                               vadj.get_lower()))
                        else:
                            vadj.set_value(min(vadj.get_value() + page,
                                               vadj.get_upper() - page))
                    return True
                # No scrollback — pass through to PTY.
                # This covers both local alt-screen apps (mc, vim) and remote
                # TUI apps over SSH (mcedit, vim, etc.) where TIOCGPGRP sees
                # "ssh" as foreground so _is_alt_screen() returns False, but
                # there's nothing to scroll locally anyway.
                return False

            # Home/End — always pass through to VTE/PTY.
            # In normal shell: readline moves cursor to beginning/end of line.
            # In alt-screen apps (mc, vim): the app handles them natively.
            # Ctrl+Home / Ctrl+End handle scrolling (see ctrl branch below).

            # --- Dangerous command interception on Enter ---
            if tab and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                msg = tab._check_dangerous_command()
                if msg:
                    tab._confirm_pending = True
                    tab._confirm_msg = msg
                    tab._confirm_bar.set_label(msg)
                    tab._sync_overlay_margins()  # ensure margin is up-to-date before showing
                    tab._confirm_bar.set_visible(True)
                    tab.terminal.grab_focus()  # keep focus for Esc
                    return True
                # Hide cursor to suppress the brief flash at col 0
                # before the shell draws the new prompt.
                tab.hide_cursor_for_enter()
            return False

        # --- Ctrl+key shortcuts (translate for non-Latin layouts) ---
        n_pages = self._notebook.get_n_pages()

        # Ctrl+F: open search and focus entry (always focus, never toggle-close)
        if latin_kv in (Gdk.KEY_f, Gdk.KEY_F) and not shift:
            if tab:
                # If there's a selection, populate search entry with it
                if tab.terminal.get_has_selection():
                    text = tab.terminal.get_text_selected(None)
                    if text:
                        # Clean up text (strip, remove newlines)
                        text = text.strip()
                        if text:
                            tab._search_entry.set_text(text)
                tab._open_search()
                return True

        # Ctrl+E: session picker over the current tab (toggles).
        # Reached even while the picker is up: the block that gives it the
        # keyboard above lets Ctrl/Alt combinations through, so this is what
        # closes it again.
        if latin_kv in (Gdk.KEY_e, Gdk.KEY_E) and not shift:
            if tab is not None:
                tab.toggle_session_picker()
            return True

        # Ctrl+T: new tab
        if latin_kv in (Gdk.KEY_t, Gdk.KEY_T):
            if self._tab_list_active or self._mru_frozen:
                # Create new tab while overlay/MRU switching is active.
                # Rebuild overlay to include the new tab and select it.
                self._add_tab()
                n = self._notebook.get_n_pages()
                if n == 0:
                    return True
                # Select the newly created tab (last page) in overlay
                if self._tab_list_active:
                    self._tab_list_selected = n - 1
                    if self._tab_list_origin_page >= n:
                        self._tab_list_origin_page = n - 1
                    # Rebuild overlay to include the new tab
                    self._show_tab_list_overlay()
                # Keep MRU cycle index in sync
                visible = self._get_visible_terminal()
                if visible and visible in self._mru_history:
                    self._mru_cycle_index = self._mru_history.index(visible)
                return True
            # Normal Ctrl+T (no switching active)
            self._add_tab()
            return True

        # Ctrl+W: close tab
        if latin_kv in (Gdk.KEY_w, Gdk.KEY_W):
            if self._tab_list_active or self._mru_frozen:
                # Close tab while overlay/MRU switching is active.
                # Close the selected (previewed) tab, not necessarily current.
                if self._tab_list_active:
                    idx = self._tab_list_selected
                    n = self._notebook.get_n_pages()
                    if idx < 0 or idx >= n:
                        return True
                    target = self._notebook.get_nth_page(idx)
                else:
                    target = self._get_visible_terminal()
                if target is None:
                    return True

                # Don't close the last tab while switching
                if self._notebook.get_n_pages() <= 1:
                    return True

                # Pick next tab to show after closing
                next_target = None
                n = self._notebook.get_n_pages()
                if self._tab_list_active:
                    page_idx = self._notebook.page_num(target)
                    if page_idx < n - 1:
                        next_target = self._notebook.get_nth_page(page_idx + 1)
                    elif page_idx > 0:
                        next_target = self._notebook.get_nth_page(page_idx - 1)
                else:
                    # MRU frozen without overlay — pick next in MRU
                    for w in self._mru_history:
                        if w is not target and self._notebook.page_num(w) >= 0:
                            next_target = w
                            break

                # Close the tab
                self.close_tab(target)

                n = self._notebook.get_n_pages()
                if n == 0:
                    return True

                # Switch to next target
                if next_target is not None and self._notebook.page_num(next_target) >= 0:
                    self._notebook.set_current_page(self._notebook.page_num(next_target))

                # Update overlay state
                if self._tab_list_active:
                    if n <= 0:
                        self._close_tab_list(activate=False)
                    else:
                        new_idx = self._notebook.page_num(next_target) if next_target else 0
                        self._tab_list_selected = max(0, min(new_idx, n - 1))
                        # Update origin_page to the tab that would be
                        # selected on the next Ctrl+Tab (MRU front).
                        if self._mru_history:
                            mru_front = self._mru_history[0]
                            mru_pg = self._notebook.page_num(mru_front)
                            if mru_pg >= 0:
                                self._tab_list_origin_page = mru_pg
                            elif n > 0:
                                self._tab_list_origin_page = min(self._tab_list_origin_page, n - 1)
                        elif n > 0:
                            self._tab_list_origin_page = min(self._tab_list_origin_page, n - 1)
                        # Rebuild overlay with updated tab list
                        self._show_tab_list_overlay()

                # Keep MRU cycle index in sync
                visible = self._get_visible_terminal()
                if visible and visible in self._mru_history:
                    self._mru_cycle_index = self._mru_history.index(visible)

                return True

            # Normal Ctrl+W (no switching active)
            cur = self._notebook.get_current_page()
            if cur >= 0:
                t = self._notebook.get_nth_page(cur)
                self.close_tab(t)
            return True

        # Ctrl+Tab / Ctrl+Shift+Tab — MRU switching
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            if not self._ctrl_tab_armed:
                return True  # ignore key-repeat
            if len(self._mru_history) >= 2:
                if not self._mru_frozen:
                    self._mru_frozen = True
                    self._mru_cycle_index = 1
                    # Schedule tab list overlay after delay on first Ctrl+Tab
                    if config.TAB_LIST_SHOW_ON_CTRL_TAB and not self._tab_list_active:
                        self._tab_list_origin_page = self._notebook.get_current_page()
                        if config.TAB_LIST_SHOW_ON_CTRL_TAB_DELAY <= 0:
                            self._tab_list_active = True
                            self._tab_list_selected = self._notebook.get_current_page()
                            self._show_tab_list_overlay()
                        elif not self._ctrl_tab_overlay_timer_id:
                            self._ctrl_tab_overlay_timer_id = GLib.timeout_add(
                                config.TAB_LIST_SHOW_ON_CTRL_TAB_DELAY,
                                self._ctrl_tab_overlay_timer_fire)
                else:
                    self._mru_cycle_index += 1
                    if self._mru_cycle_index >= len(self._mru_history):
                        self._mru_cycle_index = 0
                target = self._mru_history[self._mru_cycle_index]
                page_num = self._notebook.page_num(target)
                if page_num >= 0:
                    self._notebook.set_current_page(page_num)
                    GLib.idle_add(target.grab_terminal_focus)
                # Sync tab list selection with MRU target
                if self._tab_list_active:
                    pg = self._notebook.page_num(target)
                    if pg >= 0:
                        self._tab_list_selected = pg
                        self._move_tab_list_overlay_to(pg)
                        self._update_tab_list_selection()
            self._ctrl_tab_armed = False
            return True

        # Ctrl+Home / Ctrl+End — scroll to top/bottom of scrollback.
        # In alt-screen apps (mcedit, vim) pass through to PTY so the app
        # can handle them (e.g. mcedit uses Ctrl+Home/End to jump to
        # beginning/end of file).  In normal shell mode — scroll.
        if keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home,
                      Gdk.KEY_End, Gdk.KEY_KP_End):
            if tab and tab._is_alt_screen():
                return False  # let VTE send to PTY
            if tab:
                vadj = tab.terminal.get_vadjustment()
                if vadj:
                    if keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
                        vadj.set_value(vadj.get_lower())
                    else:
                        vadj.set_value(vadj.get_upper() - vadj.get_page_size())
            return True

        # Ctrl+Up / Ctrl+Down — open tab list
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            self._toggle_tab_list()
            return True

        # Ctrl+Left / Ctrl+Right — sequential tab navigation (deferred).
        # While Ctrl is held, we visually switch tabs but don't finalize
        # the selection (no MRU update).  The switch is committed when
        # Ctrl is released — same semantics as Ctrl+Tab MRU switching.
        if keyval == Gdk.KEY_Left:
            if not self._seq_switching:
                self._seq_switching = True
                self._seq_origin_page = self._notebook.get_current_page()
                self._mru_frozen = True  # suppress MRU updates in _on_switch_page
            cur = self._notebook.get_current_page()
            if n_pages > 1:
                # Cyclic: wrap from first tab to last
                self._notebook.set_current_page((cur - 1) % n_pages)
            return True

        if keyval == Gdk.KEY_Right:
            if not self._seq_switching:
                self._seq_switching = True
                self._seq_origin_page = self._notebook.get_current_page()
                self._mru_frozen = True  # suppress MRU updates in _on_switch_page
            cur = self._notebook.get_current_page()
            if n_pages > 1:
                # Cyclic: wrap from last tab to first
                self._notebook.set_current_page((cur + 1) % n_pages)
            return True

        return False

    def _on_key_released(self, controller, keyval, keycode, state):
        """Handle key release for MRU tab switching."""
        # Re-arm Ctrl+Tab on physical Tab release
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self._ctrl_tab_armed = True

        # Finalize tab switch when Ctrl is released
        if keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R):
            # Cancel pending overlay timer (quick Ctrl+Tab released before delay)
            self._cancel_ctrl_tab_overlay_timer()
            # Close tab list overlay on Ctrl release (activate selected)
            if self._tab_list_active and config.TAB_LIST_ACTIVATE_ON_SELECT:
                self._close_tab_list(activate=True)
            # Finalize both MRU (Ctrl+Tab) and sequential (Ctrl+Left/Right)
            need_finalize = self._mru_frozen or self._seq_switching
            self._mru_frozen = False
            self._seq_switching = False
            self._seq_origin_page = -1
            if need_finalize:
                # Push the current tab to front of MRU
                cur = self._notebook.get_current_page()
                if cur >= 0:
                    tab = self._notebook.get_nth_page(cur)
                    self._remember_tab(tab)
                    GLib.idle_add(tab.grab_terminal_focus)

    def _ctrl_tab_overlay_timer_fire(self):
        """GLib timer callback: show tab list overlay after delay."""
        self._ctrl_tab_overlay_timer_id = 0
        if self._mru_frozen and config.TAB_LIST_SHOW_ON_CTRL_TAB and not self._tab_list_active:
            self._tab_list_active = True
            # Sync selection with current MRU target (displayed tab)
            target = self._get_visible_terminal()
            if target:
                page_idx = self._notebook.page_num(target)
                if page_idx >= 0:
                    self._tab_list_selected = page_idx
            self._show_tab_list_overlay()
        return GLib.SOURCE_REMOVE

    def _cancel_ctrl_tab_overlay_timer(self):
        """Cancel pending delayed overlay timer if any."""
        if self._ctrl_tab_overlay_timer_id:
            GLib.source_remove(self._ctrl_tab_overlay_timer_id)
            self._ctrl_tab_overlay_timer_id = 0

    def _refocus_active_terminal_delayed(self):
        """Schedule focus restoration with a short delay."""
        def _do_refocus():
            if self._win is not None:
                self._win.present()
            visible = self._get_visible_terminal()
            if visible is not None and not visible._child_exited:
                visible.terminal.grab_focus()
            return False  # one-shot
        GLib.timeout_add(50, _do_refocus)

    def _on_prev_tab_clicked(self, button):
        """Switch to the previous tab."""
        cur = self._notebook.get_current_page()
        if cur > 0:
            self._notebook.set_current_page(cur - 1)
        self._update_nav_buttons_sensitivity()
        self._refocus_active_terminal_delayed()

    def _on_next_tab_clicked(self, button):
        """Switch to the next tab."""
        cur = self._notebook.get_current_page()
        n = self._notebook.get_n_pages()
        if cur < n - 1:
            self._notebook.set_current_page(cur + 1)
        self._update_nav_buttons_sensitivity()
        self._refocus_active_terminal_delayed()

    def _update_nav_buttons_visibility(self):
        """Show/hide ◀▶ nav buttons only when tabs overflow."""
        def _check():
            overflow = self._notebook_has_overflow()
            if hasattr(self, '_prev_btn'):
                self._prev_btn.set_visible(overflow)
            if hasattr(self, '_next_btn'):
                self._next_btn.set_visible(overflow)
            self._update_nav_buttons_sensitivity()
            return False  # one-shot
        GLib.idle_add(_check)

    def _update_nav_buttons_sensitivity(self):
        """Disable ◀ on first tab, ▶ on last tab."""
        cur = self._notebook.get_current_page()
        n = self._notebook.get_n_pages()
        if hasattr(self, '_prev_btn'):
            self._prev_btn.set_sensitive(cur > 0)
        if hasattr(self, '_next_btn'):
            self._next_btn.set_sensitive(cur < n - 1)

    def _notebook_has_overflow(self):
        """Check if notebook tabs overflow (need scrolling)."""
        n = self._notebook.get_n_pages()
        if n <= 1:
            return False
        # Walk internal children to find the 'tabs' container
        def _find_tabs_container(widget):
            child = widget.get_first_child() if hasattr(widget, 'get_first_child') else None
            while child is not None:
                css_classes = child.get_css_classes() if hasattr(child, 'get_css_classes') else []
                if 'top' in css_classes:
                    inner = child.get_first_child() if hasattr(child, 'get_first_child') else None
                    while inner is not None:
                        ic = inner.get_css_classes() if hasattr(inner, 'get_css_classes') else []
                        if 'tabs' in ic:
                            return inner
                        inner = inner.get_next_sibling()
                result = _find_tabs_container(child)
                if result:
                    return result
                child = child.get_next_sibling()
            return None

        tabs_box = _find_tabs_container(self._notebook)
        if tabs_box is not None:
            _, nat = tabs_box.get_preferred_size()
            actual = tabs_box.get_width()
            if actual > 0 and nat.width > actual:
                return True
            if actual > 0:
                return False
        # Fallback: assume overflow if many tabs
        return n > 6

    def _toggle_tab_list(self):
        """Toggle tab list overlay visibility."""
        if self._tab_list_active:
            self._close_tab_list(activate=True)
        else:
            self._tab_list_active = True
            self._tab_list_selected = self._notebook.get_current_page()
            self._tab_list_origin_page = self._notebook.get_current_page()
            self._show_tab_list_overlay()

    def _show_tab_list_overlay(self):
        """Build and show the tab list overlay widget."""
        # Remove old overlay if any
        self._hide_tab_list_overlay()

        n = self._notebook.get_n_pages()
        if n == 0:
            return

        cur_page = self._notebook.get_current_page()

        # Build overlay box
        overlay_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        overlay_box.add_css_class("tab-list-overlay")
        overlay_box.set_halign(Gtk.Align.END)
        overlay_box.set_valign(Gtk.Align.START)
        overlay_box.set_margin_end(20)
        overlay_box.set_margin_top(4)

        self._tab_list_buttons = []
        # During MRU/sequential switching, mark the origin tab as "active"
        # (where user started) instead of the currently displayed tab.
        active_page = self._tab_list_origin_page if self._mru_frozen else cur_page
        for i in range(n):
            tab = self._notebook.get_nth_page(i)
            title = f"Terminal {i + 1}"
            if tab._tab_label:
                title = tab._tab_label.get_label() or title

            btn = Gtk.Button(label=title)
            btn.set_has_frame(False)
            btn.add_css_class("tab-list-item")
            if i == active_page:
                btn.add_css_class("tab-list-item-active")
            if i == self._tab_list_selected:
                btn.add_css_class("tab-list-item-selected")
            btn.connect("clicked", self._on_tab_list_item_clicked, i)
            overlay_box.append(btn)
            self._tab_list_buttons.append(btn)

        # Add overlay to the current tab's overlay container
        tab = self._notebook.get_nth_page(cur_page)
        if tab and hasattr(tab, '_overlay'):
            tab._overlay.add_overlay(overlay_box)
            self._tab_list_overlay_widget = overlay_box
            self._tab_list_overlay_parent = tab._overlay

    def _hide_tab_list_overlay(self):
        """Remove tab list overlay widget."""
        widget = getattr(self, '_tab_list_overlay_widget', None)
        parent = getattr(self, '_tab_list_overlay_parent', None)
        if widget and parent:
            parent.remove_overlay(widget)
        self._tab_list_overlay_widget = None
        self._tab_list_overlay_parent = None
        self._tab_list_buttons = []

    def _close_tab_list(self, activate=False):
        """Close tab list overlay.

        activate=True: switch to selected tab.
        activate=False (Escape): restore original tab.
        """
        self._cancel_ctrl_tab_overlay_timer()
        idx = self._tab_list_selected
        n = self._notebook.get_n_pages()
        self._tab_list_active = False
        self._hide_tab_list_overlay()

        if activate and 0 <= idx < n:
            self._notebook.set_current_page(idx)
            tab = self._notebook.get_nth_page(idx)
            if tab:
                GLib.idle_add(tab.grab_terminal_focus)
        else:
            # Restore original tab
            origin = self._tab_list_origin_page
            if 0 <= origin < n:
                self._notebook.set_current_page(origin)
                tab = self._notebook.get_nth_page(origin)
                if tab:
                    GLib.idle_add(tab.grab_terminal_focus)
            else:
                tab = self._get_visible_terminal()
                if tab:
                    GLib.idle_add(tab.grab_terminal_focus)

    def _update_tab_list_selection(self):
        """Update visual selection in tab list overlay."""
        for i, btn in enumerate(getattr(self, '_tab_list_buttons', [])):
            if i == self._tab_list_selected:
                btn.add_css_class("tab-list-item-selected")
            else:
                btn.remove_css_class("tab-list-item-selected")
        # Preview: switch to selected tab and move overlay to new tab
        if config.TAB_LIST_PREVIEW:
            idx = self._tab_list_selected
            n = self._notebook.get_n_pages()
            if 0 <= idx < n:
                self._move_tab_list_overlay_to(idx)
                self._notebook.set_current_page(idx)

    def _move_tab_list_overlay_to(self, page_idx):
        """Move tab list overlay widget to a different tab's overlay."""
        widget = self._tab_list_overlay_widget
        old_parent = self._tab_list_overlay_parent
        if not widget:
            return
        new_tab = self._notebook.get_nth_page(page_idx)
        if not new_tab or not hasattr(new_tab, '_overlay'):
            return
        new_parent = new_tab._overlay
        if new_parent is old_parent:
            return  # already on the right tab
        # Detach from old parent, attach to new
        if old_parent:
            old_parent.remove_overlay(widget)
        new_parent.add_overlay(widget)
        self._tab_list_overlay_parent = new_parent

    def _on_tab_list_item_clicked(self, btn, page_idx):
        """Switch to tab from tab list and close overlay."""
        self._tab_list_selected = page_idx
        self._close_tab_list(activate=True)

    def _on_close(self, win):
        """Show bottom confirm bar before closing the window (multiple tabs)."""
        n = self._notebook.get_n_pages()
        if n <= 1:
            self._do_close()
            return False

        # Show confirm bar on the active tab — same style as dangerous-command bar
        tab = self._get_visible_terminal()
        if tab is None:
            self._do_close()
            return False

        self._close_confirm_pending = True
        msg = f"⚠  Close all {n} tabs and quit?  [Enter = quit, Esc = cancel]"
        tab._confirm_bar.set_label(msg)
        tab._sync_overlay_margins()  # ensure margin is up-to-date before showing
        tab._confirm_bar.set_visible(True)
        tab.terminal.grab_focus()  # keep focus for Esc
        return True  # suppress default close — wait for confirm

    def _do_close(self):
        """Actually clean up all tabs and quit."""
        for i in range(self._notebook.get_n_pages()):
            tab = self._notebook.get_nth_page(i)
            tab.cleanup()
        GLib.idle_add(self.quit)
        # Safety: if quit doesn't work within 500ms, force exit.
        GLib.timeout_add(500, lambda: os._exit(0) or GLib.SOURCE_REMOVE)

    def _install_fallback_icons(self):
        """Provide symbolic icons GTK widgets need when the icon theme lacks them.

        Some minimal setups have no adwaita-icon-theme, so GTK's DropDown arrow
        (pan-down-symbolic) and SpinButton +/- buttons (value-increase/
        value-decrease-symbolic) render blank — even though GTK-bundled icons
        such as window-close-symbolic still work.  We ship monochrome SVG
        fallbacks and add them to the icon search path.  If the system theme
        already provides these names, the system versions take priority and
        ours are simply never used.
        """
        icons = {
            "pan-down-symbolic":
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 16 16"><path d="M4 6l4 4 4-4z" fill="#2e3436"/></svg>',
            "value-increase-symbolic":
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 16 16"><path d="M7 3h2v4h4v2H9v4H7V9H3V7h4z" '
                'fill="#2e3436"/></svg>',
            "value-decrease-symbolic":
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 16 16"><path d="M3 7h10v2H3z" fill="#2e3436"/></svg>',
        }
        base = os.path.join(os.path.expanduser("~"), ".local", "share",
                            "cash-terminal", "icons")
        actions = os.path.join(base, "hicolor", "scalable", "actions")
        try:
            os.makedirs(actions, exist_ok=True)
            for name, svg in icons.items():
                path = os.path.join(actions, name + ".svg")
                if not os.path.isfile(path):
                    with open(path, "w") as f:
                        f.write(svg)
        except OSError:
            return
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                Gtk.IconTheme.get_for_display(display).add_search_path(base)
        except Exception:
            pass

    def _install_icons(self):
        """Install embedded SVG icon + rasterized PNGs into freedesktop paths.

        Writes:
          ~/.local/share/icons/hicolor/scalable/apps/cash-terminal.svg
          ~/.local/share/icons/hicolor/{48,64,128,256}x{size}/apps/cash-terminal.png
          ~/.local/share/applications/cash-terminal.desktop

        The rasterized PNGs ensure a crisp icon in Alt-Tab.
        """
        try:
            _svg_data = (importlib.resources.files("cash_terminal")
                         / "resources" / "cash-terminal.svg").read_bytes()
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            return

        _icons_base = os.path.join(
            os.path.expanduser("~"), ".local", "share", "icons", "hicolor")

        # 1) Install scalable SVG
        try:
            _svg_dir = os.path.join(_icons_base, "scalable", "apps")
            os.makedirs(_svg_dir, exist_ok=True)
            _svg_path = os.path.join(_svg_dir, "cash-terminal.svg")
            with open(_svg_path, "wb") as f:
                f.write(_svg_data)
        except OSError:
            pass

        # 2) Rasterize SVG to PNG at multiple sizes for Alt-Tab / WM / panel
        _png_sizes = [16, 22, 24, 32, 48, 64, 96, 128, 256]
        try:
            import gi as _gi
            _gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf as _GdkPixbuf

            for _sz in _png_sizes:
                _png_dir = os.path.join(_icons_base, f"{_sz}x{_sz}", "apps")
                os.makedirs(_png_dir, exist_ok=True)
                _png_path = os.path.join(_png_dir, "cash-terminal.png")

                _loader = _GdkPixbuf.PixbufLoader()
                _loader.set_size(_sz, _sz)
                _loader.write(_svg_data)
                _loader.close()
                _pixbuf = _loader.get_pixbuf()
                if _pixbuf:
                    _pixbuf.savev(_png_path, "png", [], [])
        except Exception:
            pass

        # 3) Install .desktop file (only if missing or changed)
        try:
            _apps_dir = os.path.join(
                os.path.expanduser("~"), ".local", "share", "applications")
            os.makedirs(_apps_dir, exist_ok=True)
            _desktop_path = os.path.join(_apps_dir, f"{APP_ID}.desktop")
            # Drop launchers left behind by earlier identifiers.
            _remove_stale_desktop_entries(_apps_dir, f"{APP_ID}.desktop")
            _desktop_content = f"""\
[Desktop Entry]
Type=Application
Name=Cash Terminal
Comment=Terminal emulator
Exec=cash-terminal
Icon=cash-terminal
Terminal=false
Categories=System;TerminalEmulator;
StartupWMClass={APP_ID}
"""
            _need_write = True
            if os.path.isfile(_desktop_path):
                with open(_desktop_path, "r") as f:
                    if f.read() == _desktop_content:
                        _need_write = False
            if _need_write:
                with open(_desktop_path, "w") as f:
                    f.write(_desktop_content)
        except OSError:
            pass

        # 4) Update icon cache (best-effort, blocking so cache is ready before window)
        try:
            import subprocess
            subprocess.run(
                ["gtk-update-icon-cache", "-f", "-t", _icons_base],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5)
        except Exception:
            pass

        return _svg_data

    @staticmethod
    def _set_x11_properties(win, svg_data):
        """Set WM_CLASS and _NET_WM_ICON X properties on the window.

        GTK4 does NOT set WM_CLASS or _NET_WM_ICON (GTK3 did both).
        - WM_CLASS is needed for Cinnamon/Muffin to match the window to
          the .desktop file (via StartupWMClass).  Without it, the DE
          creates a duplicate generic panel entry.
        - _NET_WM_ICON is needed for crisp Alt-Tab thumbnails.
        """
        try:
            import ctypes
            import ctypes.util
            import gi as _gi

            surface = win.get_surface()
            if surface is None:
                return
            display = surface.get_display()
            display_type = type(display).__name__
            if "X11" not in display_type:
                return  # Wayland — not needed

            try:
                _gi.require_version("GdkX11", "4.0")
                from gi.repository import GdkX11
                xid = surface.get_xid()
            except Exception:
                return

            _x11_path = ctypes.util.find_library("X11")
            if not _x11_path:
                return
            _libx11 = ctypes.cdll.LoadLibrary(_x11_path)

            xdisplay_raw = GdkX11.X11Display.get_xdisplay(display)
            # GdkX11.X11Display.get_xdisplay() returns a GObject-wrapped
            # pointer.  ctypes needs a plain integer (c_void_p).
            # hash() on a gi pointer object returns the raw address.
            xdisplay = ctypes.c_void_p(hash(xdisplay_raw))

            # --- 1) Set WM_CLASS via XClassHint ---
            # XClassHint struct: { char *res_name; char *res_class; }
            class XClassHint(ctypes.Structure):
                _fields_ = [
                    ("res_name", ctypes.c_char_p),
                    ("res_class", ctypes.c_char_p),
                ]

            _wm_class = APP_ID.encode()
            hint = XClassHint()
            hint.res_name = _wm_class
            hint.res_class = _wm_class
            _libx11.XSetClassHint(xdisplay, xid, ctypes.byref(hint))

            # --- 2) Set _NET_WM_ICON ---
            if svg_data:
                _gi.require_version("GdkPixbuf", "2.0")
                from gi.repository import GdkPixbuf as _GdkPixbuf

                _NET_WM_ICON = _libx11.XInternAtom(xdisplay, b"_NET_WM_ICON", False)
                XA_CARDINAL = 6

                icon_data = []
                for sz in [16, 24, 32, 48, 64, 128, 256]:
                    loader = _GdkPixbuf.PixbufLoader()
                    loader.set_size(sz, sz)
                    loader.write(svg_data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    if pixbuf is None:
                        continue
                    if not pixbuf.get_has_alpha():
                        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
                    w = pixbuf.get_width()
                    h = pixbuf.get_height()
                    rowstride = pixbuf.get_rowstride()
                    pixels = pixbuf.get_pixels()
                    n_channels = pixbuf.get_n_channels()

                    icon_data.append(w)
                    icon_data.append(h)
                    for y in range(h):
                        for x in range(w):
                            offset = y * rowstride + x * n_channels
                            r = pixels[offset]
                            g = pixels[offset + 1]
                            b = pixels[offset + 2]
                            a = pixels[offset + 3] if n_channels == 4 else 255
                            argb = (a << 24) | (r << 16) | (g << 8) | b
                            icon_data.append(argb)

                if icon_data:
                    c_data = (ctypes.c_ulong * len(icon_data))(*icon_data)
                    _libx11.XChangeProperty(
                        xdisplay, xid, _NET_WM_ICON, XA_CARDINAL, 32,
                        0,  # PropModeReplace
                        ctypes.cast(c_data, ctypes.POINTER(ctypes.c_ubyte)),
                        len(icon_data),
                    )

            _libx11.XFlush(xdisplay)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Cash Terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scheme", metavar="FILE", default=None,
        help="Color scheme YAML file (default: ~/.cash-terminal/linux.yaml)",
    )
    parser.add_argument(
        "--directory", "-d", metavar="DIR", default=None,
        help="Start in this directory",
    )
    args = parser.parse_args()

    # Ignore SIGTSTP — Ctrl+Z is captured by keyboard handler and sent to PTY.
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)

    # Ensure default config exists in ~/.cash-terminal/
    _ct_dir = os.path.join(os.path.expanduser("~"), ".cash-terminal")
    _ct_default_config = os.path.join(_ct_dir, "linux.yaml")
    if not os.path.isfile(_ct_default_config):
        try:
            os.makedirs(_ct_dir, exist_ok=True)
            _cfg_data = (importlib.resources.files("cash_terminal")
                         / "resources" / "linux.yaml").read_bytes()
            with open(_ct_default_config, "wb") as f:
                f.write(_cfg_data)
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            pass

    # Load color scheme YAML file
    if args.scheme is not None:
        scheme_file = args.scheme
        script_dir = os.path.dirname(os.path.abspath(__file__))
        for search_dir in [script_dir, os.getcwd()]:
            candidate = os.path.join(search_dir, scheme_file)
            if os.path.isfile(candidate):
                _load_yaml_config(candidate)
                break
        else:
            if os.path.isfile(scheme_file):
                _load_yaml_config(scheme_file)
    else:
        if os.path.isfile(_ct_default_config):
            _load_yaml_config(_ct_default_config)

    # Override startup directory from CLI
    if args.directory:
        d = os.path.expanduser(args.directory)
        if os.path.isdir(d):
            config.STARTUP_DIRECTORY = d

    # Set prgname BEFORE creating the Application so GTK4 sets WM_CLASS
    # to match StartupWMClass in the .desktop file.
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Cash Terminal")

    app = TerminalApp()

    # SIGINT: quit the app gracefully.
    def _sigint_handler(signum, frame):
        GLib.idle_add(app.quit)

    signal.signal(signal.SIGINT, _sigint_handler)
    app.run(None)


