"""What the mod costs the thread that draws the game, attributed by name.

A mod runs inside the client's own frame. Everything it does on the game
thread is time the frame does not have, and until this file existed there was
no way to tell which part of it: a player reporting half their usual frame
rate and a developer guessing at causes is not a diagnosis, it is a
conversation.

Measuring needs no annotation anywhere else, because a `Session` is already
the single door every piece of this mod goes through to reach the game. A
hook is a patch, a periodic job is a repeat, an event handler is a
subscription, a network answer comes back through fetch. Wrapping those five
covers the mod entirely, and nothing that is added later can forget to opt in.

What it cannot see is equally worth stating: the engine's own work, the Flash
and Gameface views the mod opens once and the client then draws every frame,
and whatever another mod does. A total here that is small while the frame
rate is halved is a real answer, not a failed measurement.
"""
import logging
import sys
import time

_logger = logging.getLogger('unicum.perf')

# Wall clock, at the finest resolution this Python offers on this platform.
# `time.time()` on Windows moves in steps of about 15 ms, which is a whole
# frame: it would report most calls as free and a few as catastrophic. Python
# 2.7's `time.clock()` is QueryPerformanceCounter there, which is what makes
# per-call numbers meaningful at all.
_now = time.clock if sys.platform == 'win32' else time.time

# Off until asked for. Every wrapper reads this before touching the clock, so
# what the mod pays when nobody is measuring is one global lookup and one
# branch per call, rather than two clock reads.
_on = False

# {name: [calls, total seconds, worst seconds]}
_totals = {}

# When the current measurement started, so a report can give a share of the
# wall clock rather than a bare total nobody can size.
_since = 0.0


def enabled():
    return _on


def start():
    """Begin measuring, discarding whatever an earlier run left."""
    global _on, _since
    _totals.clear()
    _since = _now()
    _on = True
    _logger.info('measuring what the mod costs the game thread')


def stop():
    global _on
    _on = False


def clear():
    global _since
    _totals.clear()
    _since = _now()


def track(name, func):
    """`func`, timed into the meter under `name` whenever measuring is on.

    The wrapper is installed once and kept, so measuring can be turned on
    and off in a running client without re-hooking anything. A hook that had
    to be reinstalled to be measured would be a hook that changes the thing
    it is measuring.
    """

    def measured(*args, **kwargs):
        if not _on:
            return func(*args, **kwargs)
        started = _now()
        try:
            return func(*args, **kwargs)
        finally:
            spent = _now() - started
            entry = _totals.get(name)
            if entry is None:
                _totals[name] = [1, spent, spent]
            else:
                entry[0] += 1
                entry[1] += spent
                if spent > entry[2]:
                    entry[2] = spent

    # Kept so a caller that needs the real thing (detaching an event handler
    # by identity, restoring a patch) can still reach it.
    measured.unmeasured = func
    try:
        measured.__name__ = getattr(func, '__name__', name)
    except (AttributeError, TypeError):
        pass
    return measured


def rows():
    """[(name, calls, total ms, worst ms, ms per second of wall clock)], worst first."""
    elapsed = max(_now() - _since, 1e-6)
    out = []
    for name, (calls, total, worst) in _totals.items():
        out.append((name, calls, total * 1000.0, worst * 1000.0, total * 1000.0 / elapsed))
    out.sort(key=lambda row: row[2], reverse=True)
    return out


def report(limit=15):
    """Write the table to the log, worst first.

    `per second` is the number that matters: a frame at 100 fps is 10 ms, so
    a line costing 2 ms per second of wall clock is 2 parts in a thousand of
    the thread, whatever its total. `worst` is beside it because a single
    30 ms call is a visible stutter even when its average is nothing.
    """
    measured = rows()
    elapsed = max(_now() - _since, 1e-6)
    if not measured:
        _logger.info('nothing measured in %.1f s (measuring %s)',
                     elapsed, 'on' if _on else 'OFF')
        return
    spent = sum(row[2] for row in measured)
    _logger.info('the mod spent %.1f ms of the game thread over %.1f s (%.2f%%), '
                 'across %d call(s):',
                 spent, elapsed, spent / (elapsed * 10.0), sum(row[1] for row in measured))
    _logger.info('  %-46s %8s %10s %9s %9s', 'what', 'calls', 'total ms', 'worst ms', 'ms/s')
    for name, calls, total, worst, per_second in measured[:limit]:
        _logger.info('  %-46s %8d %10.2f %9.2f %9.3f', name[:46], calls, total, worst, per_second)
    if len(measured) > limit:
        _logger.info('  ... and %d more', len(measured) - limit)
