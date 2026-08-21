"""Small stateless helpers."""
import re
from gi.repository import Gdk


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
