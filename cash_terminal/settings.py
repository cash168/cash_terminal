"""SettingsDialog — live-preview appearance editor for Cash Terminal.

Edits colours (primary fg/bg, cursor, 16 ANSI palette entries), font and
background opacity with a live preview applied to every open tab.  Save writes
the current config back to the user's YAML; Cancel restores the snapshot taken
when the dialog opened.

Colours use Gtk.ColorDialogButton (GTK 4.10+).  The preset/font pickers and
the size stepper are built from plain widgets (see _TextDropDown) so they do
not depend on the icon theme — systems without adwaita-icon-theme would
otherwise render the DropDown/SpinButton arrows blank.
"""
import os

from gi.repository import Gtk, Gdk, Pango, PangoCairo
from . import config
from . import presets


def _rgba_to_hex(rgba):
    """Gdk.RGBA → '#RRGGBB' (alpha ignored)."""
    r = max(0, min(255, round(rgba.red * 255)))
    g = max(0, min(255, round(rgba.green * 255)))
    b = max(0, min(255, round(rgba.blue * 255)))
    return f"#{r:02X}{g:02X}{b:02X}"


def _hex_to_gdk(hex_str):
    """'#RRGGBB' → Gdk.RGBA (opaque)."""
    rgba = Gdk.RGBA()
    if not rgba.parse(hex_str or "#000000"):
        rgba.parse("#000000")
    rgba.alpha = 1.0
    return rgba


class _TextDropDown(Gtk.MenuButton):
    """Icon-free dropdown: a MenuButton with a text '▾' arrow + popover list.

    Gtk.DropDown draws its arrow from the icon theme (pan-down-symbolic); on
    systems without adwaita-icon-theme that arrow renders blank.  This widget
    uses a Unicode arrow and a plain ListBox popover, so it never depends on
    the icon theme."""

    def __init__(self, items, on_select):
        super().__init__()
        self._items = list(items)
        self._on_select = on_select
        self._selected = -1
        self.set_hexpand(True)
        try:
            self.set_always_show_arrow(False)
        except Exception:
            pass

        child = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._label = Gtk.Label(xalign=0.0)
        self._label.set_hexpand(True)
        self._label.set_ellipsize(Pango.EllipsizeMode.END)
        child.append(self._label)
        child.append(Gtk.Label(label="▾"))
        self.set_child(child)

        self._popover = Gtk.Popover()
        self._popover.set_has_arrow(False)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(320)
        scroller.set_propagate_natural_height(True)
        scroller.set_propagate_natural_width(True)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for it in self._items:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label=it, xalign=0.0)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            lbl.set_margin_start(10)
            lbl.set_margin_end(10)
            row.set_child(lbl)
            self._listbox.append(row)
        scroller.set_child(self._listbox)
        self._popover.set_child(scroller)
        self.set_popover(self._popover)
        self._listbox.connect("row-activated", self._on_row_activated)

    def _on_row_activated(self, _lb, row):
        idx = row.get_index()
        self.set_selected(idx, notify=False)
        self._popover.popdown()
        if self._on_select:
            self._on_select(idx)

    def set_selected(self, idx, notify=False):
        if 0 <= idx < len(self._items):
            self._selected = idx
            self._label.set_label(self._items[idx])
            self._listbox.select_row(self._listbox.get_row_at_index(idx))
        else:
            self._selected = -1
            self._label.set_label("")
            self._listbox.unselect_all()
        if notify and self._on_select:
            self._on_select(idx)

    def get_selected(self):
        return self._selected


class SettingsDialog(Gtk.Window):
    """Modal appearance settings window with live preview + Save/Cancel."""

    def __init__(self, parent, apply_cb, on_close_cb=None):
        super().__init__(title="Настройки терминала")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(460, 640)

        # apply_cb() re-applies the current config globals to all open tabs.
        self._apply_cb = apply_cb
        # on_close_cb() lets the owner drop its reference to this dialog.
        # GTK4 has no Gtk.Widget::destroy signal, so we must notify explicitly.
        self._on_close_cb = on_close_cb
        self._closed = False

        # Snapshot every global we may touch so Cancel can restore it.
        self._snapshot = self._take_snapshot()

        # Guard to suppress live-apply while we programmatically set widgets
        # (e.g. when a preset is applied or on initial population).
        self._loading = False

        # name → ColorDialogButton, so a preset can update them all.
        self._color_buttons = {}

        # List of (row_widget, name_entry, value_entry) for the env editor.
        self._env_rows = []

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        root.append(scroller)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)
        scroller.set_child(content)

        self._build_preset_section(content)
        self._build_primary_section(content)
        self._build_palette_section(content)
        self._build_font_section(content)
        self._build_startup_section(content)
        self._build_behavior_section(content)
        self._build_tab_list_section(content)
        self._build_env_section(content)

        # Separator between scrolled content and the action buttons.
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Action buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(14)
        btn_box.set_margin_bottom(16)
        btn_box.set_margin_start(20)
        btn_box.set_margin_end(20)

        cancel_btn = Gtk.Button(label="Отмена")
        cancel_btn.connect("clicked", self._on_cancel)
        btn_box.append(cancel_btn)

        save_btn = Gtk.Button(label="Сохранить")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save)
        btn_box.append(save_btn)

        root.append(btn_box)

        # Esc cancels (restore + close).
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        self.connect("close-request", self._on_close_request)

        self._populate_from_config()
        self._sync_preset_dropdown()

    # --------------------------------------------------------- snapshot
    @staticmethod
    def _take_snapshot():
        return {
            "FG_COLOR": config.FG_COLOR,
            "BG_COLOR": config.BG_COLOR,
            "CURSOR_FG": config.CURSOR_FG,
            "CURSOR_BG": config.CURSOR_BG,
            "PALETTE_HEX": list(config.PALETTE_HEX),
            "FONT_FAMILY": config.FONT_FAMILY,
            "FONT_SIZE_PT": config.FONT_SIZE_PT,
            "BG_ALPHA": config.BG_ALPHA,
            "CHILD_ENV": dict(config.CHILD_ENV),
            "SCROLLBACK_LINES": config.SCROLLBACK_LINES,
            "STARTUP_DIRECTORY": config.STARTUP_DIRECTORY,
            "STARTUP_DIRECTORY_RAW": config.STARTUP_DIRECTORY_RAW,
            "STARTUP_FOLLOW_CWD": config.STARTUP_FOLLOW_CWD,
            "STARTUP_MAXIMIZED": config.STARTUP_MAXIMIZED,
            "STARTUP_WINDOW_SIZE": config.STARTUP_WINDOW_SIZE,
            "NEW_TAB_ACTION": config.NEW_TAB_ACTION,
            "TAB_LIST_PREVIEW": config.TAB_LIST_PREVIEW,
            "TAB_LIST_ACTIVATE_ON_SELECT": config.TAB_LIST_ACTIVATE_ON_SELECT,
            "TAB_LIST_SHOW_ON_CTRL_TAB": config.TAB_LIST_SHOW_ON_CTRL_TAB,
            "TAB_LIST_SHOW_ON_CTRL_TAB_DELAY":
                config.TAB_LIST_SHOW_ON_CTRL_TAB_DELAY,
        }

    @staticmethod
    def _restore_snapshot(snap):
        config.FG_COLOR = snap["FG_COLOR"]
        config.BG_COLOR = snap["BG_COLOR"]
        config.CURSOR_FG = snap["CURSOR_FG"]
        config.CURSOR_BG = snap["CURSOR_BG"]
        config.PALETTE_HEX = list(snap["PALETTE_HEX"])
        config.FONT_FAMILY = snap["FONT_FAMILY"]
        config.FONT_SIZE_PT = snap["FONT_SIZE_PT"]
        config.BG_ALPHA = snap["BG_ALPHA"]
        config.CHILD_ENV = dict(snap["CHILD_ENV"])
        config.SCROLLBACK_LINES = snap["SCROLLBACK_LINES"]
        config.STARTUP_DIRECTORY = snap["STARTUP_DIRECTORY"]
        config.STARTUP_DIRECTORY_RAW = snap["STARTUP_DIRECTORY_RAW"]
        config.STARTUP_FOLLOW_CWD = snap["STARTUP_FOLLOW_CWD"]
        config.STARTUP_MAXIMIZED = snap["STARTUP_MAXIMIZED"]
        config.STARTUP_WINDOW_SIZE = snap["STARTUP_WINDOW_SIZE"]
        config.NEW_TAB_ACTION = snap["NEW_TAB_ACTION"]
        config.TAB_LIST_PREVIEW = snap["TAB_LIST_PREVIEW"]
        config.TAB_LIST_ACTIVATE_ON_SELECT = snap["TAB_LIST_ACTIVATE_ON_SELECT"]
        config.TAB_LIST_SHOW_ON_CTRL_TAB = snap["TAB_LIST_SHOW_ON_CTRL_TAB"]
        config.TAB_LIST_SHOW_ON_CTRL_TAB_DELAY = \
            snap["TAB_LIST_SHOW_ON_CTRL_TAB_DELAY"]

    # --------------------------------------------------------- builders
    @staticmethod
    def _section_label(text):
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0.0)
        lbl.add_css_class("heading")
        lbl.set_markup(f"<b>{text}</b>")
        return lbl

    def _make_color_button(self, name, hex_value):
        dialog = Gtk.ColorDialog()
        dialog.set_with_alpha(False)
        btn = Gtk.ColorDialogButton.new(dialog)
        btn.set_rgba(_hex_to_gdk(hex_value))
        btn.connect("notify::rgba", self._on_color_changed, name)
        self._color_buttons[name] = btn
        return btn

    def _build_preset_section(self, parent):
        parent.append(self._section_label("Пресет"))

        self._preset_items = ["(пользовательский)"] + presets.preset_names()
        self._preset_dropdown = _TextDropDown(self._preset_items,
                                              self._on_preset_selected)
        parent.append(self._preset_dropdown)

    def _build_primary_section(self, parent):
        parent.append(self._section_label("Основные цвета"))

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        rows = [
            ("Фон", "bg"),
            ("Текст", "fg"),
            ("Курсор", "cursor_bg"),
            ("Текст под курсором", "cursor_fg"),
        ]
        values = {
            "bg": config.BG_COLOR,
            "fg": config.FG_COLOR,
            "cursor_bg": config.CURSOR_BG,
            "cursor_fg": config.CURSOR_FG,
        }
        for r, (label, name) in enumerate(rows):
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0.0)
            lbl.set_hexpand(True)
            grid.attach(lbl, 0, r, 1, 1)
            grid.attach(self._make_color_button(name, values[name]), 1, r, 1, 1)
        parent.append(grid)

    def _build_palette_section(self, parent):
        parent.append(self._section_label("Палитра ANSI"))

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        # Header row
        grid.attach(Gtk.Label(label=""), 0, 0, 1, 1)
        hn = Gtk.Label(label="Обычный")
        hb = Gtk.Label(label="Яркий")
        grid.attach(hn, 1, 0, 1, 1)
        grid.attach(hb, 2, 0, 1, 1)

        names = ["чёрный", "красный", "зелёный", "жёлтый",
                 "синий", "пурпурный", "голубой", "белый"]
        for i, cname in enumerate(names):
            lbl = Gtk.Label(label=cname)
            lbl.set_xalign(0.0)
            lbl.set_hexpand(True)
            grid.attach(lbl, 0, i + 1, 1, 1)
            grid.attach(self._make_color_button(f"normal{i}", config.PALETTE_HEX[i]),
                        1, i + 1, 1, 1)
            grid.attach(self._make_color_button(f"bright{i}", config.PALETTE_HEX[8 + i]),
                        2, i + 1, 1, 1)
        parent.append(grid)

    @staticmethod
    def _monospace_families():
        """Sorted list of monospace font family names available to Pango."""
        try:
            fm = PangoCairo.FontMap.get_default()
            names = [f.get_name() for f in fm.list_families() if f.is_monospace()]
        except Exception:
            names = []
        return sorted(set(names), key=str.lower)

    def _build_font_section(self, parent):
        parent.append(self._section_label("Шрифт и прозрачность"))

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)

        # Font family — searchable dropdown of monospace families.
        flabel = Gtk.Label(label="Шрифт")
        flabel.set_xalign(0.0)
        flabel.set_hexpand(True)
        grid.attach(flabel, 0, 0, 1, 1)

        families = self._monospace_families()
        if config.FONT_FAMILY and config.FONT_FAMILY not in families:
            families.append(config.FONT_FAMILY)
            families.sort(key=str.lower)
        self._font_families = families

        self._font_dropdown = _TextDropDown(families, self._on_font_family_changed)
        try:
            self._font_dropdown.set_selected(families.index(config.FONT_FAMILY))
        except ValueError:
            pass
        grid.attach(self._font_dropdown, 1, 0, 1, 1)

        # Font size — icon-free −/+ text stepper around a numeric entry.
        slabel = Gtk.Label(label="Размер")
        slabel.set_xalign(0.0)
        grid.attach(slabel, 0, 1, 1, 1)

        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        size_box.set_halign(Gtk.Align.START)
        dec_btn = Gtk.Button(label="−")
        dec_btn.connect("clicked", lambda _b: self._step_size(-0.5))
        size_box.append(dec_btn)

        self._size_entry = Gtk.Entry()
        self._size_entry.set_width_chars(5)
        self._size_entry.set_max_width_chars(5)
        self._size_entry.set_alignment(0.5)
        self._size_entry.connect("activate", self._on_size_entry)
        # Commit on focus-out as well as Enter.
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c: self._on_size_entry(self._size_entry))
        self._size_entry.add_controller(focus)
        size_box.append(self._size_entry)

        inc_btn = Gtk.Button(label="+")
        inc_btn.connect("clicked", lambda _b: self._step_size(0.5))
        size_box.append(inc_btn)
        grid.attach(size_box, 1, 1, 1, 1)

        # Opacity
        olabel = Gtk.Label(label="Непрозрачность фона")
        olabel.set_xalign(0.0)
        grid.attach(olabel, 0, 2, 1, 1)

        adj = Gtk.Adjustment(value=config.BG_ALPHA, lower=0.0, upper=1.0,
                             step_increment=0.05, page_increment=0.1)
        self._opacity_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                        adjustment=adj)
        self._opacity_scale.set_digits(2)
        self._opacity_scale.set_draw_value(True)
        self._opacity_scale.set_hexpand(True)
        self._opacity_scale.set_size_request(180, -1)
        self._opacity_scale.connect("value-changed", self._on_opacity_changed)
        grid.attach(self._opacity_scale, 1, 2, 1, 1)

        parent.append(grid)

    # --------------------------------------------------------- behaviour
    @staticmethod
    def _switch_row(grid, row, label, active, on_toggle):
        """Label + Gtk.Switch on one grid row.  Returns the switch.

        Gtk.Switch rather than Gtk.CheckButton on purpose: the check mark is an
        icon-theme lookup, and this dialog already goes out of its way (see
        _TextDropDown) to stay readable on systems with no icon theme.
        """
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0.0)
        lbl.set_hexpand(True)
        lbl.set_wrap(True)
        grid.attach(lbl, 0, row, 1, 1)

        sw = Gtk.Switch()
        sw.set_active(bool(active))
        sw.set_halign(Gtk.Align.END)
        sw.set_valign(Gtk.Align.CENTER)
        sw.connect("notify::active", on_toggle)
        grid.attach(sw, 1, row, 1, 1)
        return sw

    def _int_row(self, grid, row, label, value, on_commit, width=8):
        """Label + numeric entry committing on Enter and on focus-out.

        Same icon-free reasoning as _switch_row: a Gtk.SpinButton draws its
        steppers from the icon theme.
        """
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0.0)
        lbl.set_hexpand(True)
        lbl.set_wrap(True)
        grid.attach(lbl, 0, row, 1, 1)

        entry = Gtk.Entry()
        entry.set_width_chars(width)
        entry.set_max_width_chars(width)
        entry.set_halign(Gtk.Align.END)
        entry.set_alignment(1.0)
        entry.set_text(str(value))
        entry.connect("activate", on_commit)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c: on_commit(entry))
        entry.add_controller(focus)
        grid.attach(entry, 1, row, 1, 1)
        return entry

    def _text_row(self, grid, row, label, value, on_commit, placeholder=""):
        """Label + free-text entry committing on Enter and on focus-out."""
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0.0)
        lbl.set_hexpand(True)
        lbl.set_wrap(True)
        grid.attach(lbl, 0, row, 1, 1)

        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_width_chars(18)
        entry.set_text(str(value))
        if placeholder:
            entry.set_placeholder_text(placeholder)
        entry.connect("activate", on_commit)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c: on_commit(entry))
        entry.add_controller(focus)
        grid.attach(entry, 1, row, 1, 1)
        return entry

    # ----------------------------------------------------------- startup
    def _build_startup_section(self, parent):
        parent.append(self._section_label("Запуск"))

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)

        self._startup_dir_entry = self._text_row(
            grid, 0, "Рабочий каталог", config.STARTUP_DIRECTORY_RAW,
            self._on_startup_dir, placeholder="~")

        self._startup_follow_switch = self._switch_row(
            grid, 1, "Открывать новую вкладку в текущем каталоге",
            config.STARTUP_FOLLOW_CWD, self._on_startup_follow)

        self._startup_max_switch = self._switch_row(
            grid, 2, "Разворачивать окно на весь экран",
            config.STARTUP_MAXIMIZED, self._on_startup_maximized)

        self._startup_size_entry = self._text_row(
            grid, 3, "Размер окна", self._format_window_size(),
            self._on_startup_size, placeholder="auto")

        parent.append(grid)

        hint = Gtk.Label(
            label="Каталог должен существовать; «~» — домашний, переменные "
                  "вида $HOME тоже раскрываются. Когда "
                  "включено «в текущем каталоге», новая вкладка наследует "
                  "каталог активной, а заданный выше используется только для "
                  "первой вкладки окна.\n"
                  "Размер окна: «auto» — на усмотрение системы, либо "
                  "«ШИРИНАxВЫСОТА», например «960x640». Учитывается только "
                  "если окно не разворачивается на весь экран, и применяется "
                  "к новым окнам.")
        hint.set_xalign(0.0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        parent.append(hint)

        self._sync_startup_sensitivity()

    def _sync_startup_sensitivity(self):
        """A maximized window never uses the explicit size."""
        self._startup_size_entry.set_sensitive(not config.STARTUP_MAXIMIZED)

    @staticmethod
    def _format_window_size():
        size = config.STARTUP_WINDOW_SIZE
        return f"{size[0]}x{size[1]}" if size else "auto"

    def _on_startup_dir(self, entry):
        if self._loading:
            return
        text = (entry.get_text() or "").strip()
        expanded = config.expand_dir(text)
        if not expanded or not os.path.isdir(expanded):
            # Refuse rather than store: a directory that does not exist makes
            # every new tab fail to spawn, and the failure would show up far
            # from this dialog.  Put the last good value back so what is on
            # screen is always what is in effect.
            entry.set_text(config.STARTUP_DIRECTORY_RAW)
            return
        config.STARTUP_DIRECTORY_RAW = text
        config.STARTUP_DIRECTORY = expanded
        entry.set_text(text)

    def _on_startup_follow(self, switch, _pspec):
        if self._loading:
            return
        config.STARTUP_FOLLOW_CWD = switch.get_active()

    def _on_startup_maximized(self, switch, _pspec):
        if self._loading:
            return
        config.STARTUP_MAXIMIZED = switch.get_active()
        self._sync_startup_sensitivity()

    def _on_startup_size(self, entry):
        if self._loading:
            return
        text = (entry.get_text() or "").strip().lower().replace(" ", "")
        if not text or text == "auto":
            config.STARTUP_WINDOW_SIZE = None
            entry.set_text("auto")
            return
        try:
            w, h = text.split("x", 1)
            w, h = int(w), int(h)
        except ValueError:
            entry.set_text(self._format_window_size())
            return
        # The window has a 640x480 minimum (set_size_request in app.py); a
        # smaller number here would simply be ignored, so say so by clamping
        # instead of storing something that will not happen.
        w = max(640, w)
        h = max(480, h)
        config.STARTUP_WINDOW_SIZE = (w, h)
        entry.set_text(f"{w}x{h}")

    def _build_behavior_section(self, parent):
        parent.append(self._section_label("Поведение"))

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)

        self._scrollback_entry = self._int_row(
            grid, 0, "Строк в буфере прокрутки",
            int(config.SCROLLBACK_LINES), self._on_scrollback_entry)

        self._picker_switch = self._switch_row(
            grid, 1, "Открывать выбор сессии в новой вкладке",
            config.NEW_TAB_ACTION == "picker", self._on_picker_toggled)

        parent.append(grid)

        hint = Gtk.Label(
            label="Буфер прокрутки задаётся при запуске оболочки — новое "
                  "значение получат только новые вкладки.\n"
                  "Выбор сессии предлагает локальную оболочку или сохранённое "
                  "ssh-подключение; список правится в «Подключения…». Когда "
                  "переключатель выключен, новая вкладка сразу запускает "
                  "локальную оболочку.\n"
                  "Ctrl+E открывает выбор сессии в текущей вкладке в любой "
                  "момент — выбранное подключение выполняется в её оболочке, "
                  "как если бы команду набрали вручную; начатая, но не "
                  "выполненная строка при этом стирается. Пункта «Локальная "
                  "оболочка» там нет — она уже запущена в этой вкладке. "
                  "Повторный Ctrl+E или Esc закрывает меню.")
        hint.set_xalign(0.0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        parent.append(hint)

    def _on_scrollback_entry(self, entry):
        if self._loading:
            return
        text = (entry.get_text() or "").strip().replace(" ", "")
        try:
            lines = max(0, int(text))
        except ValueError:
            entry.set_text(str(int(config.SCROLLBACK_LINES)))  # last good value
            return
        config.SCROLLBACK_LINES = lines
        entry.set_text(str(lines))

    def _on_picker_toggled(self, switch, _pspec):
        if self._loading:
            return
        # NEW_TAB_COMMAND is deliberately left alone: turning the picker off
        # should restore whatever the user's "command" mode was, not reset it.
        config.NEW_TAB_ACTION = "picker" if switch.get_active() else "command"

    # --------------------------------------------------------- tab list
    def _build_tab_list_section(self, parent):
        parent.append(self._section_label("Список вкладок"))

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)

        self._tl_preview_switch = self._switch_row(
            grid, 0, "Показывать предпросмотр вкладки",
            config.TAB_LIST_PREVIEW, self._on_tl_preview)

        self._tl_activate_switch = self._switch_row(
            grid, 1, "Переключаться сразу при выборе",
            config.TAB_LIST_ACTIVATE_ON_SELECT, self._on_tl_activate)

        self._tl_ctrl_tab_switch = self._switch_row(
            grid, 2, "Открывать по Ctrl+Tab",
            config.TAB_LIST_SHOW_ON_CTRL_TAB, self._on_tl_ctrl_tab)

        self._tl_delay_entry = self._int_row(
            grid, 3, "Задержка перед показом, мс",
            int(config.TAB_LIST_SHOW_ON_CTRL_TAB_DELAY), self._on_tl_delay,
            width=6)

        parent.append(grid)

        hint = Gtk.Label(
            label="Задержка нужна, чтобы быстрое Ctrl+Tab просто переключало "
                  "вкладку, не открывая список; 0 — показывать сразу.")
        hint.set_xalign(0.0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        parent.append(hint)

        self._sync_tab_list_sensitivity()

    def _sync_tab_list_sensitivity(self):
        """The delay only means anything while Ctrl+Tab opens the list."""
        self._tl_delay_entry.set_sensitive(config.TAB_LIST_SHOW_ON_CTRL_TAB)

    def _on_tl_preview(self, switch, _pspec):
        if self._loading:
            return
        config.TAB_LIST_PREVIEW = switch.get_active()

    def _on_tl_activate(self, switch, _pspec):
        if self._loading:
            return
        config.TAB_LIST_ACTIVATE_ON_SELECT = switch.get_active()

    def _on_tl_ctrl_tab(self, switch, _pspec):
        if self._loading:
            return
        config.TAB_LIST_SHOW_ON_CTRL_TAB = switch.get_active()
        self._sync_tab_list_sensitivity()

    def _on_tl_delay(self, entry):
        if self._loading:
            return
        text = (entry.get_text() or "").strip()
        try:
            delay = max(0, int(text))
        except ValueError:
            entry.set_text(str(int(config.TAB_LIST_SHOW_ON_CTRL_TAB_DELAY)))
            return
        config.TAB_LIST_SHOW_ON_CTRL_TAB_DELAY = delay
        entry.set_text(str(delay))

    # --------------------------------------------------------- env editor
    def _build_env_section(self, parent):
        parent.append(self._section_label("Переменные окружения"))

        hint = Gtk.Label(
            label="Применяются к новым вкладкам. Пустое значение — удалить "
                  "переменную из окружения.")
        hint.set_xalign(0.0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        parent.append(hint)

        self._env_listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=6)
        parent.append(self._env_listbox)

        # One row per configured variable (insertion order preserved).
        for name, value in config.CHILD_ENV.items():
            self._add_env_row(name, "" if value is None else str(value))

        add_btn = Gtk.Button(label="＋ Добавить переменную")
        add_btn.set_halign(Gtk.Align.START)
        add_btn.connect("clicked", self._on_env_add)
        parent.append(add_btn)

    def _add_env_row(self, name="", value=""):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("Имя")
        name_entry.set_width_chars(16)
        name_entry.set_text(name or "")

        value_entry = Gtk.Entry()
        value_entry.set_placeholder_text("Значение")
        value_entry.set_hexpand(True)
        value_entry.set_text(value or "")

        del_btn = Gtk.Button(label="✕")
        del_btn.add_css_class("flat")

        row.append(name_entry)
        row.append(value_entry)
        row.append(del_btn)

        entry = (row, name_entry, value_entry)
        self._env_rows.append(entry)
        self._env_listbox.append(row)

        # Connect after setting text so initial population doesn't rebuild.
        name_entry.connect("changed", lambda _e: self._rebuild_env())
        value_entry.connect("changed", lambda _e: self._rebuild_env())
        del_btn.connect("clicked", self._on_env_remove, entry)
        return entry

    def _on_env_add(self, _btn):
        self._add_env_row("", "")

    def _on_env_remove(self, _btn, entry):
        row, _ne, _ve = entry
        try:
            self._env_rows.remove(entry)
        except ValueError:
            pass
        self._env_listbox.remove(row)
        self._rebuild_env()

    def _rebuild_env(self):
        """Rebuild config.CHILD_ENV from the editor rows.

        A row with a non-empty name and empty value stores None (remove the
        inherited variable); otherwise the value is stored as a string.  Rows
        with an empty name are ignored."""
        if self._loading:
            return
        new_env = {}
        for _row, name_entry, value_entry in self._env_rows:
            name = name_entry.get_text().strip()
            if not name:
                continue
            value = value_entry.get_text()
            new_env[name] = None if value == "" else value
        config.CHILD_ENV = new_env

    # --------------------------------------------------------- populate
    def _populate_from_config(self):
        """Set all widgets from current config (without triggering apply)."""
        self._loading = True
        try:
            self._color_buttons["bg"].set_rgba(_hex_to_gdk(config.BG_COLOR))
            self._color_buttons["fg"].set_rgba(_hex_to_gdk(config.FG_COLOR))
            self._color_buttons["cursor_bg"].set_rgba(_hex_to_gdk(config.CURSOR_BG))
            self._color_buttons["cursor_fg"].set_rgba(_hex_to_gdk(config.CURSOR_FG))
            for i in range(8):
                self._color_buttons[f"normal{i}"].set_rgba(
                    _hex_to_gdk(config.PALETTE_HEX[i]))
                self._color_buttons[f"bright{i}"].set_rgba(
                    _hex_to_gdk(config.PALETTE_HEX[8 + i]))
            try:
                self._font_dropdown.set_selected(
                    self._font_families.index(config.FONT_FAMILY))
            except (ValueError, AttributeError):
                pass
            self._size_entry.set_text(self._format_size(config.FONT_SIZE_PT))
            self._opacity_scale.set_value(config.BG_ALPHA)
            self._startup_dir_entry.set_text(config.STARTUP_DIRECTORY_RAW)
            self._startup_follow_switch.set_active(config.STARTUP_FOLLOW_CWD)
            self._startup_max_switch.set_active(config.STARTUP_MAXIMIZED)
            self._startup_size_entry.set_text(self._format_window_size())
            self._sync_startup_sensitivity()
            self._scrollback_entry.set_text(str(int(config.SCROLLBACK_LINES)))
            self._picker_switch.set_active(config.NEW_TAB_ACTION == "picker")
            self._tl_preview_switch.set_active(config.TAB_LIST_PREVIEW)
            self._tl_activate_switch.set_active(config.TAB_LIST_ACTIVATE_ON_SELECT)
            self._tl_ctrl_tab_switch.set_active(config.TAB_LIST_SHOW_ON_CTRL_TAB)
            self._tl_delay_entry.set_text(
                str(int(config.TAB_LIST_SHOW_ON_CTRL_TAB_DELAY)))
            self._sync_tab_list_sensitivity()
        finally:
            self._loading = False

    def _sync_preset_dropdown(self):
        """Select the matching preset name (or 'custom') in the dropdown."""
        self._loading = True
        try:
            name = presets.match_current(config.BG_COLOR, config.FG_COLOR,
                                         config.PALETTE_HEX)
            idx = 0
            if name and name in self._preset_items:
                idx = self._preset_items.index(name)
            self._preset_dropdown.set_selected(idx, notify=False)
        finally:
            self._loading = False

    # --------------------------------------------------------- handlers
    def _apply_live(self):
        if self._loading:
            return
        if self._apply_cb:
            self._apply_cb()

    def _on_color_changed(self, button, _pspec, name):
        if self._loading:
            return
        hex_value = _rgba_to_hex(button.get_rgba())
        if name == "bg":
            config.BG_COLOR = hex_value
        elif name == "fg":
            config.FG_COLOR = hex_value
        elif name == "cursor_bg":
            config.CURSOR_BG = hex_value
        elif name == "cursor_fg":
            config.CURSOR_FG = hex_value
        elif name.startswith("normal"):
            config.PALETTE_HEX[int(name[6:])] = hex_value
        elif name.startswith("bright"):
            config.PALETTE_HEX[8 + int(name[6:])] = hex_value
        self._apply_live()
        self._sync_preset_dropdown()

    def _on_font_family_changed(self, idx):
        if self._loading:
            return
        if 0 <= idx < len(self._font_families):
            config.FONT_FAMILY = self._font_families[idx]
            self._apply_live()

    @staticmethod
    def _format_size(pt):
        pt = float(pt)
        return str(int(pt)) if pt.is_integer() else f"{pt:.1f}"

    def _set_size(self, pt):
        """Clamp, store, reflect in the entry, and apply live."""
        pt = max(6.0, min(48.0, float(pt)))
        config.FONT_SIZE_PT = int(pt) if pt.is_integer() else round(pt, 1)
        was_loading = self._loading
        self._loading = True
        try:
            self._size_entry.set_text(self._format_size(config.FONT_SIZE_PT))
        finally:
            self._loading = was_loading
        self._apply_live()

    def _step_size(self, delta):
        if self._loading:
            return
        self._set_size(float(config.FONT_SIZE_PT) + delta)

    def _on_size_entry(self, entry):
        if self._loading:
            return
        text = (entry.get_text() or "").strip().replace(",", ".")
        try:
            pt = float(text)
        except ValueError:
            # Restore the last good value.
            entry.set_text(self._format_size(config.FONT_SIZE_PT))
            return
        self._set_size(pt)

    def _on_opacity_changed(self, scale):
        if self._loading:
            return
        config.BG_ALPHA = round(scale.get_value(), 3)
        self._apply_live()

    def _on_preset_selected(self, idx):
        if self._loading:
            return
        if idx <= 0:
            return  # "(пользовательский)"
        preset = presets.get_preset(self._preset_items[idx])
        if not preset:
            return
        config.BG_COLOR = preset["bg"]
        config.FG_COLOR = preset["fg"]
        config.CURSOR_BG = preset.get("cursor_bg", preset["fg"])
        config.CURSOR_FG = preset.get("cursor_fg", preset["bg"])
        config.PALETTE_HEX = list(preset["normal"]) + list(preset["bright"])
        self._populate_from_config()
        self._apply_live()

    # --------------------------------------------------------- save/cancel
    def _finish(self, destroy=True):
        """Notify the owner exactly once and (optionally) destroy the window.

        GTK4 has no Gtk.Widget::destroy signal, so the owning app cannot learn
        about the close any other way — we must call back explicitly."""
        if self._closed:
            return
        self._closed = True
        # Tearing the window down moves the keyboard focus, which fires the
        # focus-out commit on whichever entry had it — and on Cancel that would
        # write the abandoned text straight back over the snapshot we just
        # restored.  Every commit handler bails on _loading, so latch it here.
        self._loading = True
        if self._on_close_cb:
            try:
                self._on_close_cb()
            except Exception:
                pass
        if destroy:
            self.destroy()

    def _on_save(self, *_a):
        path = config._save_yaml_config()
        if path is None:
            dlg = Gtk.AlertDialog()
            dlg.set_message("Не удалось сохранить настройки")
            dlg.set_detail("Проверьте права доступа к файлу конфигурации.")
            dlg.show(self)
            return
        self._finish()

    def _on_cancel(self, *_a):
        self._restore_snapshot(self._snapshot)
        self._apply_live()
        self._finish()

    def _on_key(self, _ctrl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self._on_cancel()
            return True
        return False

    def _on_close_request(self, *_a):
        # Window-manager close (X) behaves like Cancel.  The window is being
        # destroyed by GTK already, so don't destroy it again here.
        if not self._closed:
            self._restore_snapshot(self._snapshot)
            self._apply_live()
            self._finish(destroy=False)
        return False
