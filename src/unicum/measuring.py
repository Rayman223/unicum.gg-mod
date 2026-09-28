"""Turns the performance meter on and off with its setting, and reports.

The meter itself (`runtime.perf`) knows how to time and how to tabulate. This
is the part that belongs to a running mod: it watches the box in the settings
window, and while the box is ticked it writes the table to the log on a timer
so the player has something to send back without doing anything but play.

Deliberately its own module rather than a corner of `__init__`: it is the one
feature whose whole purpose is to be turned on when something is wrong, so it
should be obvious where it lives.
"""
import logging

from unicum.runtime import perf, sampler

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
        # The sampler runs on a thread of its own, so it has to be stopped
        # with the session rather than left to a garbage collector that has
        # no idea a reload happened.
        self._session.on_close(sampler.stop)
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
        wanted = self._wanted()
        if wanted == self._on:
            return
        self._on = wanted
        if wanted:
            perf.start()
            # Started from here, which is the game's own thread: the sampler
            # needs to know which thread to look at, and this is the only
            # place that knows without guessing.
            sampler.start()
        else:
            # A last table before going quiet, so ticking the box off does not
            # throw away what the player just measured.
            perf.report()
            sampler.report()
            perf.stop()
            sampler.stop()
            _logger.info('stopped measuring')

    def _tick(self):
        if not self._on:
            return
        perf.report()
        # Whose code the main thread is in, which is the question our own
        # table cannot answer: it measures this mod, and a player losing
        # frames wants to know about the other twenty-eight.
        sampler.report()
        # Each table covers the interval since the last one. A running total
        # would be dominated by whatever happened in the first ten seconds
        # (a garage opening, a battle loading) long after it stopped being
        # what the player is asking about.
        perf.clear()
        sampler.clear()


def install(session, settings):
    measuring = Measuring(session, settings)
    measuring.install()
    _logger.info('installed')
    return measuring
