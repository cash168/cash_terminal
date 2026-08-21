"""Session picker — the list of things a tab can run.

It appears in two situations.  When `new_tab.action` is "picker", a new tab
does not spawn a shell right away: it shows this widget instead and waits to
be told what to run.  Ctrl+E raises the same widget over a tab that already
has a shell, and there the choice is typed into that shell instead.

The choice is either the local shell (first row, selected by default) or one
of the saved / recently used SSH connections.  The local shell row is dropped
when `include_local` is False, which is what the Ctrl+E case wants: that tab
is already running a local shell, so the row would do nothing.

The list is a plain Gtk.ListBox, so Up/Down/Home/End and Enter come for free.
Section headers ("Избранное", "История") are inserted as non-selectable rows
so arrow navigation skips over them.

The widget owns no process: it only calls `on_choose(command)` where
`command` is None for the local shell, or a full command line string
("ssh -p 2222 user@host") for a connection.  The tab does the spawning.

Escape calls `on_cancel`; what that means is the tab's business (close the
tab that has nothing running in it, or just take the overlay back down), so
the wording of the Escape hint is passed in as `cancel_hint`.  Without an
`on_cancel` it falls back to the local shell, so Escape always leads
somewhere — a picker with no way out would be a trap.
"""
from gi.repository import Gtk, Gdk, GLib, Pango

from . import connections


class SessionPicker(Gtk.Box):
    """Vertical list of session choices.  Calls on_choose() exactly once."""

    def __init__(self, on_choose, on_edit=None, on_cancel=None,
                 cancel_hint="отмена", include_local=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_choose = on_choose
        # False = no "Локальная оболочка" row; see the module docstring.
        self._include_local = include_local
        # on_edit() opens the favourites editor and leaves the picker up; the
        # owner calls reload() afterwards.  Without it a tab that starts with
        # the picker would have no way to reach the editor at all — there is
        # no shell in it yet.
        self._on_edit = on_edit
        # on_cancel() is Escape: the tab goes away instead of running anything.
        self._on_cancel = on_cancel
        self._done = False
        # Selectable rows in display order — the keyboard navigation below
        # works off this list rather than off the focused widget.
        self._rows = []
        self.add_css_class("session-picker")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(label="Выбор сессии")
        title.add_css_class("session-picker-title")
        title.set_xalign(0.0)
        self.append(title)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self._list.add_css_class("session-picker-list")
        self._list.connect("row-activated", self._on_row_activated)
        # Section headers are attached to rows via set_header_func, NOT added
        # as rows of their own: a header row would take the keyboard cursor
        # while refusing to be selected, so crossing a section boundary would
        # need two presses of the arrow key.  Headers set this way are not
        # rows at all and keyboard navigation ignores them completely.
        self._list.set_header_func(self._header_func)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(480)
        scroller.set_child(self._list)
        self.append(scroller)

        esc = cancel_hint if on_cancel is not None else "локальная оболочка"
        hint = Gtk.Label(label=f"↑↓ — выбор,  Enter — запуск,  Esc — {esc}")
        hint.add_css_class("session-picker-hint")
        hint.set_xalign(0.0)
        self.append(hint)

        self._populate()

        # Escape anywhere inside the picker cancels it.  Bound on the picker
        # itself (not the list) so it works even if focus ends up on the
        # scroller.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # ------------------------------------------------------------- building
    def _header_func(self, row, before, *_user_data):
        """Put a section header above the first row of each section.

        Called by GTK for every row whenever the list changes.  `section` is
        the label to show ("Избранное"); an empty string means "new section,
        but only a separating rule" (used before "Настроить список…").
        """
        section = getattr(row, "_section", None)
        prev = getattr(before, "_section", None) if before is not None else None
        if before is not None and section == prev:
            row.set_header(None)
            return
        if section:
            # Note this also covers the very first row: without the local shell
            # on top (Ctrl+E) a favourite opens the list, and it still needs its
            # "Избранное" header.
            label = Gtk.Label(label=section)
            label.set_xalign(0.0)
            label.add_css_class("session-picker-header")
            row.set_header(label)
        elif before is None:
            row.set_header(None)  # nothing above it to be separated from
        else:
            row.set_header(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

    def _add_choice(self, title, subtitle, command, action="run", section=None):
        """One selectable row.  `command` None means the local shell."""
        row = Gtk.ListBoxRow()
        row.add_css_class("session-picker-row")
        # The command travels on the row itself — no index bookkeeping, so
        # nothing can desynchronise.
        row._command = command
        row._action = action
        row._section = section

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        name = Gtk.Label(label=title)
        name.set_xalign(0.0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.add_css_class("session-picker-name")
        box.append(name)
        if subtitle and subtitle != title:
            sub = Gtk.Label(label=subtitle)
            sub.set_xalign(0.0)
            sub.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            sub.add_css_class("session-picker-sub")
            box.append(sub)
        row.set_child(box)
        self._list.append(row)
        self._rows.append(row)
        return row

    def _add_note(self, text):
        """A dimmed, unselectable row — an explanation, not a choice.

        Kept out of self._rows, and unselectable so GTK's own navigation skips
        it too: the arrow keys must never land on something Enter cannot run.
        """
        row = Gtk.ListBoxRow()
        row.add_css_class("session-picker-row")
        row.set_selectable(False)
        row.set_activatable(False)
        row._section = None
        label = Gtk.Label(label=text)
        label.set_xalign(0.0)
        label.add_css_class("session-picker-sub")
        row.set_child(label)
        self._list.append(row)
        return row

    def _populate(self):
        self._rows = []
        if self._include_local:
            self._add_choice("Локальная оболочка", "", None)

        # A group entry has no row of its own: its name simply becomes the
        # section of everything that follows, and _header_func draws it above
        # the first of those rows.  A group with nothing after it therefore
        # renders as nothing at all, which is the right answer for an empty
        # group — and there is no dead row for the arrow keys to trip over.
        fav_section = "Избранное"
        fav_cmds = set()
        for fav in connections.load_favorites():
            if fav["type"] == "group":
                fav_section = fav["name"]
                continue
            self._add_choice(fav["name"], fav["command"], fav["command"],
                             section=fav_section)
            fav_cmds.add(fav["command"])

        history = connections.load_history(limit=connections.PICKER_HISTORY)
        # Do not repeat a connection that is already in the favourites above.
        history = [h for h in history if h["command"] not in fav_cmds]
        for item in history:
            self._add_choice(item["target"], item["command"], item["command"],
                             section="История")

        # Without the local shell row the list can come up with nothing in it
        # at all (Ctrl+E on a machine with no saved connections yet).  Say so,
        # otherwise the picker looks broken rather than empty.
        if not self._rows:
            self._add_note("Нет сохранённых подключений")

        if self._on_edit is not None:
            self._add_choice("Настроить список…", "", None,
                             action="edit", section="")

        if self._rows:
            self._list.select_row(self._rows[0])

    def reload(self):
        """Rebuild the list after the favourites were edited."""
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt
        self._populate()
        self.grab_picker_focus()

    # -------------------------------------------------------------- focus
    def grab_picker_focus(self):
        """Focus the list so the arrow keys work immediately."""
        row = self._list.get_selected_row()
        if row is not None:
            row.grab_focus()
        else:
            self._list.grab_focus()

    # ------------------------------------------------------------- events
    def handle_key(self, keyval):
        """Drive the picker from the window-level key controller.

        The picker is an overlay on top of the terminal, so a click next to it
        (or any stray key the ListBox does not consume) moves the keyboard
        focus elsewhere and the arrows stop reaching the list.  The window
        controller sees every key regardless of focus, so navigation is done
        here instead — and every other key is swallowed, because a tab that
        has no shell yet has nothing to send them to.

        Always returns True: while the picker is up it owns the keyboard.
        """
        if keyval == Gdk.KEY_Escape:
            self._cancel()
        elif keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move(-1)
        elif keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move(1)
        elif keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            self._select_index(0)
        elif keyval in (Gdk.KEY_End, Gdk.KEY_KP_End):
            self._select_index(len(self._rows) - 1)
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            row = self._list.get_selected_row()
            if row is not None:
                self._on_row_activated(self._list, row)
        return True

    def _move(self, delta):
        row = self._list.get_selected_row()
        try:
            idx = self._rows.index(row)
        except ValueError:
            idx = 0 if delta > 0 else len(self._rows) - 1
            self._select_index(idx)
            return
        self._select_index(idx + delta)

    def _select_index(self, idx):
        """Select row `idx`, clamped.  Also pulls the focus back into the list,
        so the arrows restore it after a click landed on the terminal."""
        if not self._rows:
            return
        idx = max(0, min(len(self._rows) - 1, idx))
        row = self._rows[idx]
        self._list.select_row(row)
        row.grab_focus()  # scrolls the row into view as a side effect

    def _on_key(self, _ctrl, keyval, _keycode, _state):
        # Fallback for the case where the window controller is not in play
        # (e.g. focus inside the picker while no window handler ran).
        return self.handle_key(keyval)

    def _on_row_activated(self, _list, row):
        if getattr(row, "_action", "run") == "edit":
            if self._on_edit is not None:
                self._on_edit()
            return
        self._choose(getattr(row, "_command", None))

    def _choose(self, command):
        """Fire the callback once and only once."""
        if self._done:
            return
        self._done = True
        # Deferred so the picker can be torn down from inside the callback
        # while GTK is still dispatching this key/activate event.
        GLib.idle_add(self._on_choose, command)

    def _cancel(self):
        """Escape.  Same one-shot / deferred discipline as _choose().

        Deferring matters more here than for _choose(): the callback destroys
        the tab this widget lives in, and doing that while GTK is still
        dispatching the key event would pull the ground out from under it.
        """
        if self._on_cancel is None:
            self._choose(None)
            return
        if self._done:
            return
        self._done = True
        GLib.idle_add(self._on_cancel)
