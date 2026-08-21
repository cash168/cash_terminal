"""ConnectionsDialog — editor for the session picker's favourites + history.

Favourites are an ordered list the user maintains by hand (name + command
line); history is filled automatically by the tab-title poll every time an ssh
process is seen.  The dialog edits the first and lets you promote entries of
the second into it.

Nothing is written until Сохранить: the favourite rows and the "clear history"
flag are applied together, so Отмена really cancels.  Storage lives in
connections.py (~/.cash-terminal/connections.yaml) — this file is pure UI.
"""
import shlex
import time

from gi.repository import Gtk, Gdk, Pango

from . import connections


class ConnectionsDialog(Gtk.Window):
    """Modal favourites/history editor with Save/Cancel."""

    def __init__(self, parent, on_close_cb=None):
        super().__init__(title="Подключения")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(620, 640)

        self._on_close_cb = on_close_cb
        self._closed = False
        # (row_widget, name_entry, command_entry, kind) per favourite row, in
        # list order; command_entry is None for a group.
        self._fav_rows = []
        # (button, command) per history row — must exist before the favourites
        # section is built, since adding a row syncs these buttons.
        self._hist_buttons = []
        # The favourite row currently being dragged, and the index its drop
        # indicator is drawn at.  Both None while no drag is in progress.
        self._drag_entry = None
        self._drop_indicator = None
        # History is only cleared on Save, so Отмена can still back out.
        self._clear_history = False

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

        self._build_favorites_section(content)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self._build_history_section(content)

        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

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

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        self.connect("close-request", self._on_close_request)

    # --------------------------------------------------------- section: favourites
    @staticmethod
    def _section_label(text):
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0.0)
        lbl.add_css_class("heading")
        lbl.set_markup(f"<b>{text}</b>")
        return lbl

    def _build_favorites_section(self, parent):
        parent.append(self._section_label("Избранное"))

        hint = Gtk.Label(
            label="Команда запускается напрямую, без оболочки — например "
                  "«ssh -p 2222 user@host». Порядок строк = порядок в меню "
                  "выбора сессии; строки переставляются перетаскиванием за "
                  "⠿ или Ctrl+↑ / Ctrl+↓. Группа — заголовок над идущими за "
                  "ней строками, до следующей группы.")
        hint.set_xalign(0.0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        parent.append(hint)

        self._fav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        parent.append(self._fav_box)

        # One drop target for the whole list rather than one per row.  GTK
        # walks up from the widget under the pointer looking for a target that
        # accepts the offered type, so this also catches drops landing on the
        # entries inside a row — and the entries, which accept text drops, do
        # not steal ours because the payload is a GtkBox, not a string.
        drop = Gtk.DropTarget.new(Gtk.Box, Gdk.DragAction.MOVE)
        drop.connect("motion", self._on_fav_drag_motion)
        drop.connect("leave", self._on_fav_drag_leave)
        drop.connect("drop", self._on_fav_drop)
        self._fav_box.add_controller(drop)

        self._fav_empty = Gtk.Label(label="Список пуст.")
        self._fav_empty.set_xalign(0.0)
        self._fav_empty.add_css_class("dim-label")
        parent.append(self._fav_empty)

        for fav in connections.load_favorites():
            self._add_fav_row(fav["name"], fav.get("command", ""),
                              kind=fav["type"])

        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add_box.set_halign(Gtk.Align.START)

        add_btn = Gtk.Button(label="＋ Добавить подключение")
        add_btn.connect("clicked", lambda _b: self._add_fav_row("", "", focus=True))
        add_box.append(add_btn)

        group_btn = Gtk.Button(label="＋ Добавить группу")
        group_btn.connect(
            "clicked", lambda _b: self._add_fav_row("", "", focus=True,
                                                    kind="group"))
        add_box.append(group_btn)
        parent.append(add_box)

        self._sync_fav_empty()

    def _add_fav_row(self, name="", command="", focus=False, kind="item"):
        """One editor row: a connection (name + command) or a group title.

        A group row has no command entry at all — `cmd_entry` is None, and
        that is what every consumer branches on.
        """
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        handle = Gtk.Label(label="⠿")
        handle.add_css_class("dim-label")
        handle.add_css_class("connections-drag-handle")
        handle.set_tooltip_text("Потяните, чтобы переставить "
                                "(или Ctrl+↑ / Ctrl+↓)")
        handle.set_cursor(Gdk.Cursor.new_from_name("grab", None))
        row.append(handle)

        name_entry = Gtk.Entry()
        name_entry.set_text(name or "")

        if kind == "group":
            tag = Gtk.Label(label="▤")
            tag.add_css_class("dim-label")
            tag.set_tooltip_text("Группа")
            name_entry.set_placeholder_text("Название группы")
            name_entry.set_hexpand(True)
            cmd_entry = None
            row.append(tag)
            row.append(name_entry)
        else:
            name_entry.set_placeholder_text("Название")
            name_entry.set_width_chars(16)
            cmd_entry = Gtk.Entry()
            cmd_entry.set_placeholder_text("ssh user@host")
            cmd_entry.set_hexpand(True)
            cmd_entry.set_text(command or "")
            # Connected after set_text so building the editor does not sync.
            cmd_entry.connect("changed", lambda _e: self._sync_promote_buttons())
            row.append(name_entry)
            row.append(cmd_entry)

        del_btn = Gtk.Button(label="✕")
        del_btn.add_css_class("flat")
        del_btn.set_tooltip_text("Удалить")
        row.append(del_btn)

        entry = (row, name_entry, cmd_entry, kind)
        self._fav_rows.append(entry)
        self._fav_box.append(row)

        del_btn.connect("clicked", lambda _b: self._remove_fav_row(entry))

        # Dragging starts on the handle only: the row is full of entries, and a
        # drag source on the row itself would swallow the click-and-drag that
        # selects text inside them.
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_fav_drag_prepare, entry)
        drag.connect("drag-begin", self._on_fav_drag_begin, entry)
        drag.connect("drag-end", self._on_fav_drag_end, entry)
        handle.add_controller(drag)

        # Keyboard equivalent of the drag, since the ▲▼ buttons are gone.
        # Bound on the row, so it fires while the focus is in either entry.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_fav_row_key, entry)
        row.add_controller(keys)

        self._sync_fav_empty()
        self._sync_promote_buttons()
        if focus:
            name_entry.grab_focus()
        return entry

    def _remove_fav_row(self, entry):
        row = entry[0]
        try:
            self._fav_rows.remove(entry)
        except ValueError:
            return
        self._fav_box.remove(row)
        # The indicator is an index into the list that just changed length.
        self._clear_drop_indicator()
        self._sync_fav_empty()
        self._sync_promote_buttons()

    def _move_fav_row(self, entry, delta):
        """Reorder one row by one position (Ctrl+↑ / Ctrl+↓)."""
        try:
            idx = self._fav_rows.index(entry)
        except ValueError:
            return
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(self._fav_rows):
            return
        self._fav_rows[idx], self._fav_rows[new_idx] = (
            self._fav_rows[new_idx], self._fav_rows[idx])
        self._reanchor_fav_rows()

    def _reanchor_fav_rows(self):
        """Re-stack the GTK box to match `self._fav_rows`.

        Every row is re-anchored in list order rather than patching only the
        ones that moved: reorder_child_after(child, sibling) with a running
        "previous" is trivially correct, and these lists are short.
        """
        prev = None
        for fav_entry in self._fav_rows:
            row = fav_entry[0]
            self._fav_box.reorder_child_after(row, prev)
            prev = row

    def _on_fav_row_key(self, _ctrl, keyval, _keycode, state, entry):
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move_fav_row(entry, -1)
            entry[1].grab_focus()
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move_fav_row(entry, 1)
            entry[1].grab_focus()
            return True
        return False

    def _sync_fav_empty(self):
        self._fav_empty.set_visible(not self._fav_rows)

    # --------------------------------------------------------- drag and drop
    def _on_fav_drag_prepare(self, _source, _x, _y, entry):
        """Offer the row widget itself as the payload.

        A GtkBox rather than a string on purpose: the row is full of GtkEntry
        widgets, which come with a text drop target of their own and would
        happily eat a text payload dropped on them.
        """
        self._drag_entry = entry
        return Gdk.ContentProvider.new_for_value(entry[0])

    def _on_fav_drag_begin(self, source, _drag, entry):
        row = entry[0]
        # Drag icon = a picture of the row, so what follows the pointer is the
        # thing being moved rather than the little handle it was grabbed by.
        paintable = Gtk.WidgetPaintable.new(row)
        source.set_icon(paintable, 10, row.get_height() // 2)
        row.add_css_class("connections-dragging")

    def _on_fav_drag_end(self, _source, _drag, _delete_data, entry):
        entry[0].remove_css_class("connections-dragging")
        self._drag_entry = None
        self._clear_drop_indicator()

    def _drop_index_at(self, y):
        """Where a drop at height `y` would insert, as an index into _fav_rows.

        A row counts as "passed" once the pointer is below its middle, so the
        insertion point flips over exactly when the dragged row would visually
        swap with it.  Coordinates are asked of GTK per call instead of being
        cached: rows have different heights (a group row is shorter) and the
        list can be reordered mid-drag.
        """
        for idx, fav_entry in enumerate(self._fav_rows):
            ok, rect = fav_entry[0].compute_bounds(self._fav_box)
            if not ok:
                continue
            if y < rect.origin.y + rect.size.height / 2:
                return idx
        return len(self._fav_rows)

    def _show_drop_indicator(self, idx):
        """Draw the insertion line, as a border on the row next to it."""
        if self._drop_indicator == idx:
            return
        self._clear_drop_indicator()
        if not self._fav_rows:
            return
        if idx < len(self._fav_rows):
            self._fav_rows[idx][0].add_css_class("connections-drop-above")
        else:
            self._fav_rows[-1][0].add_css_class("connections-drop-below")
        self._drop_indicator = idx

    def _clear_drop_indicator(self):
        for fav_entry in self._fav_rows:
            fav_entry[0].remove_css_class("connections-drop-above")
            fav_entry[0].remove_css_class("connections-drop-below")
        self._drop_indicator = None

    def _on_fav_drag_motion(self, _target, _x, y):
        if self._drag_entry is None:
            # A drag from somewhere else — refuse it rather than reordering.
            return Gdk.DragAction(0)
        self._show_drop_indicator(self._drop_index_at(y))
        return Gdk.DragAction.MOVE

    def _on_fav_drag_leave(self, _target):
        self._clear_drop_indicator()

    def _on_fav_drop(self, _target, value, _x, y):
        entry = self._drag_entry
        if entry is None:
            # Fall back to matching the payload widget, in case drag-end ran
            # first (the order of drop vs. drag-end is not guaranteed).
            entry = next((e for e in self._fav_rows if e[0] is value), None)
        self._clear_drop_indicator()
        if entry is None:
            return False
        try:
            old = self._fav_rows.index(entry)
        except ValueError:
            return False
        new = self._drop_index_at(y)
        # The index was computed with the row still in the list, so pulling it
        # out shifts everything below it up by one.
        if new > old:
            new -= 1
        if new == old:
            return True
        self._fav_rows.pop(old)
        self._fav_rows.insert(new, entry)
        self._reanchor_fav_rows()
        return True

    # --------------------------------------------------------- section: history
    def _build_history_section(self, parent):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = self._section_label("История подключений")
        title.set_hexpand(True)
        header.append(title)
        clear_btn = Gtk.Button(label="Очистить")
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", self._on_clear_history)
        header.append(clear_btn)
        parent.append(header)

        hint = Gtk.Label(
            label="Заполняется автоматически при каждом ssh-подключении.")
        hint.set_xalign(0.0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        parent.append(hint)

        self._hist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        parent.append(self._hist_box)

        self._hist_empty = Gtk.Label(label="История пуста.")
        self._hist_empty.set_xalign(0.0)
        self._hist_empty.add_css_class("dim-label")
        parent.append(self._hist_empty)

        self._populate_history()

    def _populate_history(self):
        child = self._hist_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._hist_box.remove(child)
            child = nxt

        self._hist_buttons = []
        items = [] if self._clear_history else connections.load_history()
        self._hist_empty.set_visible(not items)
        for item in items:
            self._hist_box.append(self._make_history_row(item))
        self._sync_promote_buttons()

    def _make_history_row(self, item):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        text.set_hexpand(True)

        cmd = Gtk.Label(label=item["command"])
        cmd.set_xalign(0.0)
        cmd.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        text.append(cmd)

        when = ""
        if item.get("last"):
            try:
                when = time.strftime("%d.%m.%Y %H:%M",
                                     time.localtime(item["last"]))
            except (ValueError, OSError):
                when = ""
        meta = f"{item['target']} · {item['count']}× · {when}".strip(" ·")
        sub = Gtk.Label(label=meta)
        sub.set_xalign(0.0)
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.add_css_class("dim-label")
        text.append(sub)

        row.append(text)

        star = Gtk.Button(label="★ В избранное")
        star.add_css_class("flat")
        star.connect("clicked", self._on_promote, item)
        row.append(star)
        self._hist_buttons.append((star, item["command"]))
        return row

    def _sync_promote_buttons(self):
        """Grey out "В избранное" for connections that are already there.

        Recomputed from the live editor rows rather than tracked per click:
        the favourites can also change by hand-editing a command, deleting a
        row or adding one, and a per-click flag would go stale on all three.
        """
        present = {cmd_entry.get_text().strip()
                   for _row, _name, cmd_entry, _kind in self._fav_rows
                   if cmd_entry is not None}
        for btn, command in self._hist_buttons:
            already = command in present
            btn.set_sensitive(not already)
            btn.set_label("★ В избранном" if already else "★ В избранное")

    def _on_promote(self, _btn, item):
        """Copy a history entry into the favourites editor."""
        self._add_fav_row(self._suggest_name(item), item["command"])

    @staticmethod
    def _suggest_name(item):
        """A short human name for a promoted history entry.

        `target` is what the tab title shows ("(user) host"); when it is not
        available fall back to the last argument of the command line, which for
        ssh is the destination.
        """
        target = (item.get("target") or "").strip()
        if target and target != item["command"]:
            return target
        try:
            argv = shlex.split(item["command"])
        except ValueError:
            argv = item["command"].split()
        return argv[-1] if argv else item["command"]

    def _on_clear_history(self, _btn):
        self._clear_history = True
        self._populate_history()

    # --------------------------------------------------------- save / close
    def _collect_favorites(self):
        out = []
        for _row, name_entry, cmd_entry, kind in self._fav_rows:
            name = name_entry.get_text().strip()
            if kind == "group":
                if name:  # an untitled group would be an invisible separator
                    out.append({"type": "group", "name": name})
                continue
            command = cmd_entry.get_text().strip()
            if not command:
                continue  # a row with no command is not a connection
            out.append({"name": name or command, "command": command})
        return out

    def _on_save(self, _btn):
        connections.save_favorites(self._collect_favorites())
        if self._clear_history:
            connections.clear_history()
        self._finish()

    def _on_cancel(self, _btn):
        self._finish()

    def _on_key(self, _ctrl, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self._finish()
            return True
        return False

    def _on_close_request(self, *_a):
        self._notify_closed()
        return False

    def _finish(self):
        self._notify_closed()
        self.close()

    def _notify_closed(self):
        if self._closed:
            return
        self._closed = True
        if self._on_close_cb:
            self._on_close_cb()
