"""Session picker — the list of things a tab can run.

It appears in two situations.  When `new_tab.action` is "picker", a new tab
does not spawn a shell right away: it shows this widget instead and waits to
be told what to run.  Ctrl+E raises the same widget over a tab that already
has a shell, and there the choice is typed into that shell instead.

The choice is either the local shell (first row, selected by default) or one
of the saved / recently used SSH connections.  The local shell row is dropped
when `include_local` is False, which is what the Ctrl+E case wants: that tab
is already running a local shell, so the row would do nothing.

The list is a plain Gtk.ListBox.  Section headers ("Избранное", "История") are
inserted as non-selectable rows so arrow navigation skips over them.

A search entry sits above the list and holds the keyboard focus from the moment
the picker opens, so the list can be narrowed by just typing.  ↑↓ and Enter keep
driving the list while it does; everything else is the entry's.  The filter
matches the connection name and its command line, and never hides
"Настроить список…" — a query that matches nothing must not also hide the way to
fix the list.

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
from .util import scroll_into_view


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
        # Every selectable row in display order, and the subset the filter
        # currently lets through.  Keyboard navigation works off _rows rather
        # than off the focused widget, so it must never contain a hidden row.
        self._all_rows = []
        self._rows = []
        # The two explanatory rows, shown one at a time or not at all.
        self._empty_row = None
        self._no_match_row = None
        # Coalesced scroll-to-row, see _scroll_to_row().
        self._scroll_id = 0
        self._scroll_row = None
        self.add_css_class("session-picker")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(label="Выбор сессии")
        title.add_css_class("session-picker-title")
        title.set_xalign(0.0)
        self.append(title)

        # A plain Gtk.Entry, not Gtk.SearchEntry: the latter draws a magnifier
        # and a clear button from the icon theme, and both come up blank on a
        # system without adwaita-icon-theme — the same reason the Settings
        # dialog builds its pickers out of plain widgets.
        self._search = Gtk.Entry()
        self._search.set_placeholder_text("Поиск…")
        self._search.add_css_class("session-picker-search")
        self._search.connect("changed", lambda _e: self._apply_filter())
        self.append(self._search)

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
        # The filter reads a flag the rows carry, rather than re-deriving the
        # match here: _apply_filter() has to build the navigation list anyway,
        # and deciding it in one place keeps the two from disagreeing.
        self._list.set_filter_func(lambda row: getattr(row, "_visible", True))

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_propagate_natural_height(True)
        self._scroller.set_max_content_height(480)
        self._scroller.set_child(self._list)
        _sadj = self._scroller.get_vadjustment()
        if _sadj is not None:
            _sadj.connect("changed", self._on_scroller_adj_changed)
        self.append(self._scroller)

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
        # What the search matches against: the visible name plus the command
        # line, so "2222" or a hostname finds a favourite named "prod".
        row._search_text = f"{title} {subtitle or ''}".lower()
        row._visible = True

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
        self._all_rows.append(row)
        return row

    def _add_note(self, text):
        """A dimmed, unselectable row — an explanation, not a choice.

        Kept out of self._all_rows, and unselectable so GTK's own navigation
        skips it too: the arrow keys must never land on something Enter cannot
        run.
        """
        row = Gtk.ListBoxRow()
        row.add_css_class("session-picker-row")
        row.set_selectable(False)
        row.set_activatable(False)
        row._section = None
        row._visible = True
        label = Gtk.Label(label=text)
        label.set_xalign(0.0)
        label.add_css_class("session-picker-sub")
        row.set_child(label)
        self._list.append(row)
        return row

    def _populate(self):
        self._all_rows = []
        self._rows = []
        self._empty_row = None
        self._no_match_row = None
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
        if not self._all_rows:
            self._empty_row = self._add_note("Нет сохранённых подключений")

        # Built up front and hidden, rather than added and removed as the query
        # changes: the list is only rebuilt when the favourites are edited, and
        # touching its children on every keystroke would re-run the header
        # function over the whole list each time.
        self._no_match_row = self._add_note("Ничего не найдено")

        if self._on_edit is not None:
            self._add_choice("Настроить список…", "", None,
                             action="edit", section="")

        self._apply_filter()

    # ------------------------------------------------------------- filtering
    def _apply_filter(self):
        """Re-run the search filter and fix up the selection.

        Rebuilds self._rows so the arrow keys only ever walk visible rows, and
        pulls the selection onto a visible row when the one it was on has just
        been filtered out.
        """
        query = self._search.get_text().strip().lower()
        for row in self._all_rows:
            if getattr(row, "_action", "run") == "edit":
                row._visible = True
            else:
                row._visible = (not query) or (query in row._search_text)
        self._rows = [r for r in self._all_rows if r._visible]

        # "Настроить список…" is always visible, so it cannot stand in for a
        # match — count only real choices when deciding what to explain.
        matches = [r for r in self._rows
                   if getattr(r, "_action", "run") != "edit"]
        if self._empty_row is not None:
            self._empty_row._visible = not query
        if self._no_match_row is not None:
            self._no_match_row._visible = bool(query) and not matches
        self._list.invalidate_filter()

        selected = self._list.get_selected_row()
        if selected is None or not getattr(selected, "_visible", False):
            if self._rows:
                self._select_index(0)
            else:
                self._list.unselect_all()

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
        """Focus the search entry so typing narrows the list immediately.

        The list needs no focus of its own: ↑↓ and Enter are routed here by the
        window-level key controller regardless of what holds it (see
        handle_key), which is what lets the entry keep it the whole time.
        """
        self._search.grab_focus()

    def _search_has_focus(self):
        """FOCUS_WITHIN, not has_focus(): a Gtk.Entry delegates the focus to an
        inner GtkText, so the Entry itself never reports having it."""
        return bool(self._search.get_state_flags() & Gtk.StateFlags.FOCUS_WITHIN)

    # ------------------------------------------------------------- events
    def handle_key(self, keyval):
        """Drive the picker from the window-level key controller.

        The picker is an overlay on top of the terminal, so a click next to it
        (or any stray key the ListBox does not consume) moves the keyboard
        focus elsewhere and the arrows stop reaching the list.  The window
        controller sees every key regardless of focus, so navigation is done
        here instead.

        Returns True for the keys that drive the list, and False for the rest so
        they reach the search entry — Home/End/←/→/BackSpace are the entry's
        text keys now, and space types a space instead of launching.
        """
        if keyval == Gdk.KEY_Escape:
            self._cancel()
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move(-1)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move(1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            row = self._list.get_selected_row()
            if row is not None:
                self._on_row_activated(self._list, row)
            return True
        if keyval == Gdk.KEY_Tab:
            # Nothing else in the picker is worth focusing, and letting Tab move
            # the focus off the entry would silently stop the search working.
            return True

        if self._search_has_focus():
            return False

        # The picker is an overlay: a click beside it lands on the terminal
        # underneath and takes the focus with it.  Returning False now would
        # type the character into that shell, so pull the focus back first and
        # apply the character here — the alternative is losing the first
        # keystroke after every stray click.
        self._search.grab_focus()
        unichar = Gdk.keyval_to_unicode(keyval)
        if unichar:
            char = chr(unichar)
            if char.isprintable():
                self._search.set_text(self._search.get_text() + char)
                self._search.set_position(-1)
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
        """Select row `idx`, clamped, and scroll it into view."""
        if not self._rows:
            return
        idx = max(0, min(len(self._rows) - 1, idx))
        row = self._rows[idx]
        self._list.select_row(row)
        self._scroll_to_row(row)

    def _scroll_to_row(self, row):
        """Reveal `row` without touching the keyboard focus.

        row.grab_focus() used to do this as a side effect, but the focus belongs
        to the search entry now and taking it away would swallow the next
        character typed.

        Deferred to idle because the row's position is not known yet at the two
        moments this matters: on the first open the scroller has no allocation,
        and straight after invalidate_filter() the rows have not been laid out
        again.  Coalesced to one pending callback so holding ↓ does not queue one
        per keypress; the newest row wins.

        The request survives a failed attempt: idle callbacks all run before the
        frame that allocates the scroller, so the first try can find a page size
        of 0 and have nothing to measure against.  The adjustment's "changed"
        signal then finishes the job once layout lands.
        """
        self._scroll_row = row
        if self._scroll_id:
            return
        self._scroll_id = GLib.idle_add(self._scroll_idle)

    def _scroll_idle(self):
        self._scroll_id = 0
        self._try_scroll()
        return GLib.SOURCE_REMOVE

    def _on_scroller_adj_changed(self, _adj):
        """Layout is happening — retry the pending scroll once it has finished.

        Deliberately not measuring right here: "changed" arrives in the middle of
        the layout pass, when the scroller has its geometry but the rows inside
        have not been placed, so compute_bounds() would hand back a stale
        rectangle and the bogus result would count as a success.  An idle runs
        between frames, after the allocation is done.
        """
        if self._scroll_row is None:
            return
        if self._scroll_id:
            return
        self._scroll_id = GLib.idle_add(self._scroll_idle)

    def _try_scroll(self):
        """Scroll to the pending row, dropping the request only on success."""
        row = self._scroll_row
        if row is None:
            return
        if scroll_into_view(self._scroller, row, margin=4):
            self._scroll_row = None

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
