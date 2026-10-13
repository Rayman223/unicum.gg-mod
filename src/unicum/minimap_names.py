"""The player's name beside their vehicle on the minimap.

The client writes the VEHICLE there -- "Tiger-Maus", "E 75" -- and nothing
else, which is often not what a player needs: calling out who is pushing a
flank wants the name of the person, not the model of their tank.

One method decides that label. `ArenaVehiclesPlugin._getDisplayedName(vInfo)`
returns `vInfo.getDisplayedName()`, and everything the minimap draws for a
vehicle goes through it: the first draw, every respawn, and each time a
vehicle is spotted again. Patching it is therefore the whole of this module.

**It only ever replaces a label the client was going to write anyway.** The
plugin calls this inside `_setVehicleInfo`, which the client skips entirely
when the player has minimap labels turned off in the game's own settings, so
our setting cannot put names on a minimap that was meant to stay bare.

**The name is the one the client itself shows.** `player_format.format` builds
a player's label with `vInfoVO.getDisplayedName(vInfoVO.player.getPlayerLabel())`
and that exact line is used here: it keeps the client's handling of bots, of
players with no name, and of whatever the client decides to show in a replay,
rather than reading `player.name` raw and risking showing something the game
had chosen to hide.
"""
import logging

_logger = logging.getLogger('unicum.minimap_names')

# How a vehicle and a player are written together, when both are asked for.
# The vehicle first, because that is where the eye already goes on a minimap
# that has always shown it there.
_BOTH = u'%s %s'


def _label(vInfo, choice):
    """What to write for this vehicle, or None to leave the client's label be."""
    if choice == 'vehicle':
        return None
    player = getattr(vInfo, 'player', None)
    if player is None:
        return None
    # The client's own line for "this player's name", bots included.
    name = vInfo.getDisplayedName(player.getPlayerLabel())
    if not name:
        return None
    if choice == 'player':
        return name
    vehicle = vInfo.getDisplayedName()
    # Nothing to join when the client had no vehicle name to give.
    return _BOTH % (vehicle, name) if vehicle else name


class MinimapNames(object):

    def __init__(self, session, settings):
        self._session = session
        self._settings = settings

    def install(self):
        try:
            from gui.Scaleform.daapi.view.battle.shared.minimap.plugins import ArenaVehiclesPlugin
        except ImportError:
            _logger.info('no minimap vehicles plugin in this client, labels are the client\'s')
            return
        self._session.patch(ArenaVehiclesPlugin, '_getDisplayedName', self._wrap)
        _logger.info('installed on ArenaVehiclesPlugin._getDisplayedName')

    def _wrap(self, original):
        settings = self._settings

        def _getDisplayedName(plugin, vInfo):
            # The client's answer is what we fall back to, so a fault here
            # costs a label rather than the minimap: this runs for every
            # vehicle every time one is spotted.
            try:
                if settings['enabled']:
                    found = _label(vInfo, settings.minimap_names())
                    if found:
                        return found
            except Exception:
                _logger.exception('could not label a vehicle on the minimap')
            return original(plugin, vInfo)

        return _getDisplayedName


def install(session, settings):
    MinimapNames(session, settings).install()
