"""The resource id a Gameface view needs, by whichever road this client offers.

A view the mod draws in the game's modern UI -- the tank page button, the
Twitch window, the ratings on the results screen -- has to name a *layout id*,
a number the client assigns to the HTML it will load. Getting that number has
changed hands:

  native      Client 2.4.1.0 reads `gui/gen_resources/*.json` itself, in C++,
              before any mod's Python runs, and publishes each entry under
              `R.mods.<...>`. This is the road the client asks for: it logs
              "res_map modification is handled natively, please refrain from
              using it and migrate to native R.* accessors" the moment the old
              one is touched.
  openwg      Before that, net.openwg.gameface merged `mods/configs/res_map/`
              entries into the client's map and answered `res_id_by_key`. On
              2.4.1.0 it still imports and still answers, but the answer is
              INVALID_RES_ID, which reads exactly like "not registered yet" --
              which is how this looked like a missing file for an afternoon.

Both are tried, native first, because a release targets one client while
players sit on several: the package is published per game version, but a
client can lag a day behind its folder and nothing here should care.

The mod ships BOTH declarations (`res/gui/gen_resources/unicum.json` and
`res/mods/configs/res_map/unicum.json`). They describe the same two HTML files
and only one of them is ever read by a given client, so the cost is a few
hundred bytes and the gain is that neither client is a special case.
"""
import logging

_logger = logging.getLogger('unicum.layouts')

# What `INVALID_RES_ID` means, without importing it. It lived in
# gui.impl.gen_utils, which 2.4.1.0 keeps but which a later client may not, and
# a module-level import of a name that disappears takes the whole package down.
# It has been -1 for as long as the resource system has existed.
_INVALID = -1


def _native(key):
    """The id published under `R`, or None when this client has no such entry.

    `key` is the dotted path the declaration gives, `R.mods...` included, so
    the JSON and the code name the same thing and a typo shows up as a log line
    rather than as a view that silently never opens.
    """
    try:
        from gui.impl.gen import R
    except ImportError:
        return None
    node = R
    for step in key.split('.')[1:]:  # past the leading R
        node = getattr(node, step, None)
        if node is None:
            return None
    try:
        found = node()
    except TypeError:
        # A branch rather than a leaf: the declaration and the client disagree
        # about the shape of this path, which is worth saying out loud.
        _logger.warning('%s is not a layout id in this client', key)
        return None
    return found if found != _INVALID else None


def _openwg(feature):
    """The id openwg_gameface answers for `feature`, or None."""
    try:
        from openwg_gameface import res_id_by_key
    except ImportError:
        return None
    found = res_id_by_key(feature)
    return found if found != _INVALID else None


def layout_id(key, feature):
    """The layout id for a view, or None when this client has not got one.

    None is the honest answer and the caller says what is lost by it. It
    happens for real: on a fresh install the resource system has not seen the
    mod's folder yet, and the entry only exists from the next client start.
    """
    found = _native(key)
    if found is not None:
        return found
    found = _openwg(feature)
    if found is not None:
        _logger.info('%s came from openwg_gameface; this client has no native entry', feature)
    return found
