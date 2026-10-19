"""In-window popups: the Ctrl+Tab switcher, the tab list and the confirm card.

All three are drawn by this terminal, not by the toolkit's dialogs, and share
one visual language — the ct-* CSS classes in app.py: a caps header, 1px
dividers, rows with an inset rounded selection, a darker footer with key caps.
The settings window borrows the same classes, so everything the terminal puts
in front of the user looks like one family.

They hang on the window-level overlay rather than on a tab.  The tabs under
them change while they are open (Ctrl+Tab and the list's preview really switch
tabs), and a widget hung on one tab's own overlay has to be re-parented each
time — which resets its scroll position and was the root of a run of bugs.

The widgets are pure presentation.  They own no tabs and handle no keys: the
application drives them from its window-level key controller and gets mouse
actions back through callbacks.  Those callbacks are always delivered from
idle, never from inside the click: closing a tab rebuilds the rows, and tearing
down the row whose button is still dispatching its click trips GTK's "Broken
accounting of active state" warning.

No icon-theme widgets anywhere — the close button is a plain "×" label, the
current-tab mark a plain "✓" — for the same reason the Settings dialog avoids
them: systems without adwaita-icon-theme render those icons blank.
"""
from gi.repository import Gtk, GLib, Pango


def _defer(callback, *args):
    """Run callback(*args) from idle, after the current event is dispatched."""
    def _run():
        callback(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(_run)


def _keycap(text):
    cap = Gtk.Label(label=text)
    cap.add_css_class("ct-key")
    return cap


def _hint(text, end=False):
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("ct-hint")
    if end:
        lbl.add_css_class("end")
    return lbl


def _header(title, trailing=None):
    """Caps header row.  Upper case is written out rather than left to CSS
    text-transform, which not every GTK 4 release honours."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    box.add_css_class("ct-popup-header")
    lbl = Gtk.Label(label=title.upper(), xalign=0.0)
    lbl.set_hexpand(True)
    lbl.add_css_class("ct-popup-title")
    box.append(lbl)
    if trailing is not None:
        trailing.add_css_class("ct-popup-title")
        box.append(trailing)
    return box


# ---------------------------------------------------------------------------
#  Tab rows: shared by the switcher and the tab list
# ---------------------------------------------------------------------------
class _TabRows(Gtk.Box):
    """A scrollable list of tab rows with one selected row.

    Rows are built from entry dicts: number (position in the tab bar, 1-based),
    title, detail (may be empty) and current (bool).  How `current` and the
    number are shown is the subclass's call.

    Every row has the same height (CSS min-height, one line of text), which is
    what makes the scroll arithmetic below exact.
    """

    # Subclass switches.
    SHOW_NUMBER = False      # number badge on the left
    CHECK_CURRENT = False    # ✓ on the left of the current tab
    LABEL_CURRENT = False    # "текущая" before the × button
    DETAIL_RIGHT = False     # detail right-aligned instead of after the title
    COMPACT = False          # 26px rows instead of 32px

    def __init__(self, on_select, on_close):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_select = on_select
        self._on_close = on_close
        self._rows = []
        self._selected = 0
        self._scroll_pending = False
        self._scroll_id = 0
        self._scroll_tries = 0
        self.set_focusable(False)
        self.add_css_class("ct-popup")

        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._list.add_css_class("ct-rows")
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_propagate_natural_height(True)
        # Takes layout space instead of floating over the × buttons.
        self._scroller.set_overlay_scrolling(False)
        self._scroller.set_child(self._list)
        adj = self._scroller.get_vadjustment()
        if adj is not None:
            adj.connect("changed", self._on_adj_changed)

    def set_limits(self, width, list_height):
        """Fixed width, and the list height past which it scrolls."""
        self.set_size_request(width, -1)
        self._scroller.set_max_content_height(max(1, list_height))

    # ------------------------------------------------------------- building
    def set_entries(self, entries, selected):
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt
        self._rows = [self._make_row(i, e) for i, e in enumerate(entries)]
        for row in self._rows:
            self._list.append(row)
        self._entries_changed(entries)
        self.set_selected(selected)

    def _entries_changed(self, entries):
        """Hook for subclasses (the switcher updates its count)."""

    def _make_row(self, index, entry):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.add_css_class("ct-row")
        if self.COMPACT:
            row.add_css_class("compact")
        row.set_cursor_from_name("pointer")

        if self.SHOW_NUMBER:
            num = Gtk.Label(label=str(entry["number"]))
            num.add_css_class("ct-row-num")
            num.set_valign(Gtk.Align.CENTER)
            row.append(num)
        if self.CHECK_CURRENT:
            mark = Gtk.Label(label="✓" if entry.get("current") else "")
            mark.add_css_class("ct-row-check")
            row.append(mark)

        name = Gtk.Label(label=entry["title"], xalign=0.0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.add_css_class("ct-row-name")
        row.append(name)

        detail = Gtk.Label(label=entry.get("detail") or "",
                           xalign=1.0 if self.DETAIL_RIGHT else 0.0)
        detail.set_hexpand(True)
        # The tail of a path is the part that tells two tabs apart.
        detail.set_ellipsize(Pango.EllipsizeMode.START)
        detail.add_css_class("ct-row-detail")
        row.append(detail)

        if self.LABEL_CURRENT and entry.get("current"):
            cur = Gtk.Label(label="текущая")
            cur.add_css_class("ct-row-current")
            row.append(cur)

        close = Gtk.Button(label="×")
        close.set_has_frame(False)
        close.set_focusable(False)
        close.set_valign(Gtk.Align.CENTER)
        close.add_css_class("ct-row-close")
        close.set_tooltip_text("Закрыть вкладку")
        close.connect("clicked", lambda _b, i=index: _defer(self._on_close, i))
        row.append(close)

        # The × button claims its own clicks, so this gesture only ever sees a
        # click on the rest of the row.
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released",
                      lambda _g, _n, _x, _y, i=index: _defer(self._on_select, i))
        row.add_controller(click)
        return row

    # ------------------------------------------------------------ selection
    def set_selected(self, index):
        if not self._rows:
            return
        index = max(0, min(len(self._rows) - 1, index))
        for i, row in enumerate(self._rows):
            if i == index:
                row.add_css_class("selected")
            else:
                row.remove_css_class("selected")
        self._selected = index
        self._queue_scroll()

    # ------------------------------------------------------------ scrolling
    # Nothing can be measured or written from inside layout.  An idle runs
    # before the frame that allocates the scroller (page size still 0), and
    # "changed" fires in the middle of size-allocate, where a written value is
    # re-clamped away.  So the request stays pending until a read-back proves
    # the value took, and "changed" only ever queues the idle.
    def _queue_scroll(self):
        self._scroll_pending = True
        self._scroll_tries = 0
        if not self._scroll_id:
            self._scroll_id = GLib.idle_add(self._scroll_idle)

    def _on_adj_changed(self, _adj):
        if self._scroll_pending and not self._scroll_id:
            self._scroll_id = GLib.idle_add(self._scroll_idle)

    def _scroll_idle(self):
        self._scroll_id = 0
        self._try_scroll()
        return GLib.SOURCE_REMOVE

    def _try_scroll(self):
        """Scroll the selected row into view; clears the request on success.

        Rows are uniform, so upper / count is the row height exactly and no
        child has to be measured — measuring is what cannot be trusted here.
        """
        adj = self._scroller.get_vadjustment()
        if adj is None or not self._rows:
            self._scroll_pending = False
            return
        page = adj.get_page_size()
        upper = adj.get_upper()
        if page <= 0 or upper <= 0:
            return                      # not laid out yet: stay pending
        if upper <= page:
            self._scroll_pending = False
            return

        row_h = upper / len(self._rows)
        top = self._selected * row_h
        bottom = top + row_h
        value = adj.get_value()
        # A sliver of the neighbouring row stays visible: that edge is what
        # shows there is more list past the selection.
        margin = min(6.0, row_h / 2)
        target = value
        if top - margin < value:
            target = max(0.0, top - margin)
        elif bottom + margin > value + page:
            target = min(upper - page, bottom + margin - page)
        if target != value:
            adj.set_value(target)
            if abs(adj.get_value() - target) > 0.5:
                # Geometry is already valid, so "changed" may never come again
                # to retry — re-arm here, bounded: the target is clamped into
                # range, so a write that keeps failing will not start working.
                self._scroll_tries += 1
                if self._scroll_tries < 8 and not self._scroll_id:
                    self._scroll_id = GLib.idle_add(self._scroll_idle)
                else:
                    self._scroll_pending = False
                return
        self._scroll_pending = False

    def dispose_pending(self):
        """Drop a queued scroll before the widget is taken off screen."""
        if self._scroll_id:
            GLib.source_remove(self._scroll_id)
            self._scroll_id = 0
        self._scroll_pending = False


class TabSwitcher(_TabRows):
    """Ctrl+Tab: the open tabs in most-recently-used order.

    Shown while Ctrl is held after Ctrl+Tab, once a short delay has passed (a
    quick tap flips to the previous tab without ever drawing it).  The order is
    the order Ctrl+Tab walks in, so every press moves the highlight one row; the
    number on the left is the tab's position in the tab bar — where you will
    end up.  It hangs from the top, has a caps header and a key-cap footer, and
    lives only while Ctrl is down, so it cannot be mistaken for the session
    picker (a centred card with a search entry that waits for Enter).
    """

    SHOW_NUMBER = True
    LABEL_CURRENT = True

    def __init__(self, on_select, on_close):
        super().__init__(on_select, on_close)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.START)

        self._count = Gtk.Label()
        self.append(_header("Открытые вкладки · сначала недавние", self._count))
        self.append(self._scroller)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        footer.add_css_class("ct-popup-footer")
        for keys, text in (("Tab", "далее"), ("Shift+Tab", "назад"),
                           ("↑↓ ←→", None), ("Del", "закрыть вкладку")):
            footer.append(_keycap(keys))
            if text:
                footer.append(_hint(text))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        footer.append(spacer)
        footer.append(_hint("Отпустите Ctrl, чтобы открыть", end=True))
        self.append(footer)

    def _entries_changed(self, entries):
        self._count.set_label(str(len(entries)))


class TabListPopup(_TabRows):
    """The tab list dropped from the ▼ button (and Ctrl+Up/Down).

    Tabs in tab-bar order, the active one marked with ✓, the directory
    right-aligned, × on every row.  No header or footer: it is a menu, and a
    click on a row picks that tab.
    """

    CHECK_CURRENT = True
    DETAIL_RIGHT = True
    COMPACT = True

    def __init__(self, on_select, on_close):
        super().__init__(on_select, on_close)
        self.add_css_class("ct-menu")
        self.set_halign(Gtk.Align.END)
        self.set_valign(Gtk.Align.START)
        self.append(self._scroller)


# ---------------------------------------------------------------------------
#  Confirm card
# ---------------------------------------------------------------------------
class ConfirmPopup(Gtk.Box):
    """A modal question drawn inside the window: a dimmed backdrop over the
    whole window and a card in the middle of it.

    Built for "close the window with several tabs open" — that used to be a bar
    along the bottom of the terminal, easy to read as one more line of output.
    A card over a dimmed window cannot be missed, and the backdrop also keeps
    clicks from reaching the terminal underneath while the question is open.

    `lines` are shown under the message (e.g. the tabs that still run
    something).  on_answer(True/False) is called once, from idle.  Enter and
    Escape are routed here by the application's key controller; a click on the
    backdrop counts as Escape.
    """

    def __init__(self, title, message, lines, confirm_label, on_answer,
                 cancel_label="Отмена"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_answer = on_answer
        self._answered = False
        self.add_css_class("ct-scrim")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)

        backdrop_click = Gtk.GestureClick()
        backdrop_click.connect("released", self._on_backdrop_click)
        self.add_controller(backdrop_click)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("ct-popup")
        card.add_css_class("ct-card")
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)
        card.set_vexpand(True)
        card.set_size_request(440, -1)
        # Clicks on the card itself must not count as clicks on the backdrop.
        card_click = Gtk.GestureClick()
        card_click.connect("released", lambda g, *_a: g.set_state(
            Gtk.EventSequenceState.CLAIMED))
        card.add_controller(card_click)
        self._card = card

        card.append(_header(title))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.add_css_class("ct-card-body")
        msg = Gtk.Label(label=message, xalign=0.0)
        msg.set_wrap(True)
        msg.set_max_width_chars(50)
        msg.add_css_class("ct-card-text")
        body.append(msg)
        for text in lines or ():
            line = Gtk.Label(label=text, xalign=0.0)
            line.set_ellipsize(Pango.EllipsizeMode.END)
            line.add_css_class("ct-card-line")
            body.append(line)
        card.append(body)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        footer.add_css_class("ct-popup-footer")
        footer.append(_keycap("Enter"))
        footer.append(_hint(confirm_label.lower()))
        footer.append(_keycap("Esc"))
        footer.append(_hint(cancel_label.lower()))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        footer.append(spacer)
        cancel = Gtk.Button(label=cancel_label)
        cancel.add_css_class("ct-button")
        cancel.set_focusable(False)
        cancel.connect("clicked", lambda _b: self.answer(False))
        footer.append(cancel)
        ok = Gtk.Button(label=confirm_label)
        ok.add_css_class("ct-button")
        ok.add_css_class("accent")
        ok.set_focusable(False)
        ok.connect("clicked", lambda _b: self.answer(True))
        footer.append(ok)
        card.append(footer)

        self.append(card)

    def _on_backdrop_click(self, gesture, _n, x, y):
        # The card claims its own clicks; anything that reaches here outside
        # the card's rectangle is a click on the dimmed backdrop.
        ok, rect = self._card.compute_bounds(self)
        if ok and (rect.origin.x <= x <= rect.origin.x + rect.size.width
                   and rect.origin.y <= y <= rect.origin.y + rect.size.height):
            return
        self.answer(False)

    def answer(self, accepted):
        """Answer once; the callback runs from idle (see the module notes)."""
        if self._answered:
            return
        self._answered = True
        _defer(self._on_answer, bool(accepted))
