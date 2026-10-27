"""Turns the performance meter on and off with its setting, and reports.

The meter itself (`runtime.perf`) knows how to time and how to tabulate. This
is the part that belongs to a running mod: it watches the box in the settings
window, and while the box is ticked it writes the table to the log on a timer
so the player has something to send back without doing anything but play.

Deliberately its own module rather than a corner of `__init__`: it is the one
feature whose whole purpose is to be turned on when something is wrong, so it
should be obvious where it lives.

What it measures is this mod, and only this mod. There used to be a sampler
beside it that read the main thread's Python stack from a second thread, to
say which of a player's mods the thread was actually in. It worked, and it
broke the game: see `_follow`.
"""
import logging

from unicum.runtime import perf

_logger = logging.getLogger('unicum.measuring')

# Long enough that the table is worth reading and short enough that a player
# can turn it on, play one minute and send six of them.
_EVERY = 10.0


class Measuring(object):
    """Follows the setting; reports while it is on."""

    def __init__(self, session, settings):
        self._session = session
        self._settings = settings
        self._on = False

    def install(self):
        self._settings.on_change(self._follow)
        self._follow()
        # One timer for the life of the session rather than one started and
        # stopped with the box: a repeat cannot be cancelled on its own, and
        # a tick that finds the meter off costs nothing.
        self._session.repeat(_EVERY, self._tick)

    def _wanted(self):
        try:
            return self._settings.measures_performance()
        except Exception:
            _logger.exception('could not read the measuring setting')
            return False

    def _follow(self):
        """Start or stop with the box.

        `perf` and nothing else. A sampler used to start here too, reading
        the main thread's stack from a daemon thread via
        `sys._current_frames()` to attribute the thread to one mod or
        another. Do not bring it back:

        reading another thread's frames means holding references to that
        thread's live locals while it runs, and this client keeps engine
        components in them. The components are owned by C++ and must not be
        referenced from Python past their scope, so the engine started
        refusing them by the hundred --

            [ERROR] [C++] [CGF_SCRIPT] Component: <Vehicular.VehicleTracks>
                    was stored in python, please fix it

        -- and the game showed it: tanks without tracks, turrets that would
        not turn, a garage camera that would not behave. Measured on
        2026-10-27: zero such errors in a sixteen-hour log before the
        sampler was switched on, 426 in the eighty minutes after, and none
        again once it stopped. Tidying the references cannot fix it; the
        hazard is reading a running thread's frames at all.

        Which mod is costing a player frames is a real question, and this is
        not an answer to it.
        """
        wanted = self._wanted()
        if wanted == self._on:
            return
        self._on = wanted
        if wanted:
            perf.start()
        else:
            # A last table before going quiet, so ticking the box off does not
            # throw away what the player just measured.
            perf.report()
            perf.stop()
            _logger.info('stopped measuring')

    def _tick(self):
        if not self._on:
            return
        perf.report()
        # Each table covers the interval since the last one. A running total
        # would be dominated by whatever happened in the first ten seconds
        # (a garage opening, a battle loading) long after it stopped being
        # what the player is asking about.
        perf.clear()


def install(session, settings):
    measuring = Measuring(session, settings)
    measuring.install()
    _logger.info('installed')
    return measuring
