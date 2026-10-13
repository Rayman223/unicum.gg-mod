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
import weakref

from unicum.extended_info import waits

_logger = logging.getLogger('unicum.minimap_names')

# How a vehicle and a player are written together, when both are asked for.
#
# The vehicle first, because that is where the eye already goes on a minimap
# that has always shown it there, and the player in brackets: run together,
# "Conqueror _Winnie" reads as one long vehicle name, which is exactly what it
# looked like the first time it was tried.
_BOTH = u'%s (%s)'


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

    def __init__(self, session, settings, alt=None):
        self._session = session
        self._settings = settings
        # The extended-info key (Alt by default), when this surface is set to
        # wait for it. Shared with every other surface that does.
        self._alt = alt
        # The plugins of the battle being drawn. Weak, because a plugin lives
        # and dies with its battle and this module outlives both.
        self._plugins = weakref.WeakSet()

    def install(self):
        try:
            from gui.Scaleform.daapi.view.battle.shared.minimap.plugins import ArenaVehiclesPlugin
        except ImportError:
            _logger.info('no minimap vehicles plugin in this client, labels are the client\'s')
            return
        self._session.patch(ArenaVehiclesPlugin, '_getDisplayedName', self._wrap)
        self._settings.on_change(self._redraw)
        if self._alt is not None:
            # Holding the key has to relabel what is already drawn, exactly as
            # changing the setting does: the client asks for a label when a
            # vehicle appears, not when a key moves.
            self._alt.on_change(self._redraw)
        _logger.info('installed on ArenaVehiclesPlugin._getDisplayedName')

    def _wrap(self, original):
        settings = self._settings
        plugins = self._plugins
        this = self

        def _getDisplayedName(plugin, vInfo):
            # The client's answer is what we fall back to, so a fault here
            # costs a label rather than the minimap: this runs for every
            # vehicle every time one is spotted.
            try:
                plugins.add(plugin)
                if settings['enabled'] and not this._waiting():
                    found = _label(vInfo, settings.minimap_names())
                    if found:
                        return found
            except Exception:
                _logger.exception('could not label a vehicle on the minimap')
            return original(plugin, vInfo)

        return _getDisplayedName

    def _waiting(self):
        """Whether this surface is set to wait for the key and it is not held.

        The same rule and the same key as every other surface of the mod, so a
        player who set "Only with Alt" once gets it here too.
        """
        return waits(self._alt, self._settings, 'minimap')

    def _redraw(self, *args):
        """Relabel the vehicles already on the minimap, after a setting changed.

        The client asks for a label when it first draws a vehicle and each time
        one is spotted again, so without this a change made mid-battle would
        only reach a tank the next time it appeared -- and a tank sitting still
        in the open would keep the old label for the rest of the game.

        Entry by entry, deliberately. The plugin exposes no "redraw them all",
        and the one the statistics controller offers taught us what a broad
        invalidation costs: it reinjects whole collections and takes other
        mods' work with it (see `battle.py` `_redraw`). This re-sends exactly
        what the client sends when a vehicle moves, for the vehicles already
        drawn, and touches nothing else.
        """
        arena = _arena()
        if arena is None:
            return
        for plugin in list(self._plugins):
            entries = getattr(plugin, '_entries', None)
            if not entries:
                continue
            for vehicleID, entry in list(entries.items()):
                try:
                    vInfo = arena.getVehicleInfo(vehicleID)
                    if vInfo is None or vInfo.isObserver():
                        continue
                    plugin._setVehicleInfo(
                        vehicleID, entry, vInfo,
                        arena.getPlayerGuiProps(vehicleID, vInfo.team))
                except Exception:
                    # Not debug: a relabel that fails is invisible otherwise,
                    # and that is how the broken arena lookup above went
                    # unnoticed. Once per vehicle at worst, on a setting
                    # change, so the log can afford it.
                    _logger.exception('could not relabel %s on the minimap',
                                      vehicleID)


def _arena():
    """The battle's arena data provider, or None outside a battle.

    `getArenaDP()` and not `shared.arenaDP`: the second is what I assumed and
    it does not exist, so this returned None every time and the relabel below
    did nothing at all, silently. Same call as `battle.py` uses.
    """
    try:
        from helpers import dependency
        from skeletons.gui.battle_session import IBattleSessionProvider
        return dependency.instance(IBattleSessionProvider).getArenaDP()
    except Exception:
        _logger.debug('no arena to relabel from', exc_info=True)
        return None


def install(session, settings, alt=None):
    MinimapNames(session, settings, alt).install()
