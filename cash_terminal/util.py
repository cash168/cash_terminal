"""Small stateless helpers."""
import re
from gi.repository import Gdk


def scroll_into_view(scroller, widget, margin=0):
    """Scroll `scroller` the minimum amount that makes `widget` fully visible.

    Both lists that can outgrow their window — the tab list overlay and the
    session picker — need this, and neither can use the usual trick of calling
    grab_focus() on the row: the tab list is driven while the keyboard focus
    belongs to the terminal, and in the picker the focus belongs to the search
    entry.  So the offset is computed by hand.

    Returns False when the scroller has not been allocated yet (its page size
    is still 0, so there is no viewport to scroll within) — the caller is
    expected to be running from an idle callback and can simply try again.

    `margin` keeps that many pixels of the neighbouring row visible, which is
    what tells the eye there is more list in that direction.
    """
    if scroller is None or widget is None:
        return False
    adj = scroller.get_vadjustment()
    if adj is None:
        return False
    page = adj.get_page_size()
    if page <= 0:
        return False
    # Measured against the scroller, not against its child: a ScrolledWindow
    # wraps a child that is not GtkScrollable (a Box, a ListBox) in a viewport
    # of its own, and then get_child() does not reliably name the widget whose
    # coordinates are the scrolled content.  Relative to the scroller the
    # numbers are simply viewport coordinates — already offset by the current
    # scroll — so a negative top means "above the view" and a bottom past the
    # page size means "below it", whatever GTK put in between.
    ok, rect = widget.compute_bounds(scroller)
    if not ok or rect.size.height <= 0:
        # A zero-height rectangle means the row has not been allocated yet.
        # Treating that as a real position would scroll to a meaningless offset
        # and, worse, report success — so the caller would stop retrying.
        return False
    top = rect.origin.y - margin
    bottom = rect.origin.y + rect.size.height + margin
    value = adj.get_value()
    if top < 0:
        adj.set_value(max(adj.get_lower(), value + top))
    elif bottom > page:
        adj.set_value(min(adj.get_upper() - page, value + bottom - page))
    return True


def _translate_keyval_to_latin(keycode, keyval):
    """Map a non-Latin keyval to its Latin equivalent using group 0.

    When the keyboard layout is e.g. Russian, pressing the physical 'T' key
    produces keyval=Cyrillic_ie (е) instead of KEY_t.  Hotkeys like Ctrl+T
    stop working because the handler checks for KEY_t/KEY_T.
    """
    if (Gdk.KEY_a <= keyval <= Gdk.KEY_z or
            Gdk.KEY_A <= keyval <= Gdk.KEY_Z or
            Gdk.KEY_0 <= keyval <= Gdk.KEY_9):
        return keyval
    display = Gdk.Display.get_default()
    if display is None:
        return keyval
    result = display.translate_key(keycode, Gdk.ModifierType(0), 0)
    if result[0]:
        return result[1]
    return keyval


# Pre-compiled regex for stripping OSC 52 clipboard-write sequences.
_RE_OSC52 = re.compile(rb'\x1b\]52;[^\x07\x1b]*(?:\x07|\x1b\\)')
