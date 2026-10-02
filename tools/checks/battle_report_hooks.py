"""Checks for how the capture attaches to the client's battle results.

Separate from the checks for what a report is read from, because this is the
other half of the same failure and it failed on its own. A real client reported
`onResultPosted` as installed and then never fired it: nothing was captured and
nothing was said, because the install took the first way in that existed and
returned, leaving the `postResult` patch that reaches the same moment unapplied.
"""
import os
import sys
import tempfile

from checks.battle_fixtures import Bonus, Link, Settings, results
from checks.common import check


class _Event(object):
    """A WG Event, reduced to attaching and firing."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, posted):
        for handler in list(self.handlers):
            handler(posted)


class _Both(object):
    """A service offering both ways in."""

    def __init__(self):
        self.onResultPosted = _Event()

    def postResult(self, result, *args):
        return 'the client answer'


class _EventOnly(object):
    def __init__(self):
        self.onResultPosted = _Event()


class _Neither(object):
    pass


def _constants():
    """Give the fake client the bonus types it does not carry.

    The hooks reach `capture` the way the client does, without the bonus types
    passed in, so `modes.mode_of` imports them from the client itself. Every
    other check hands them over explicitly, which is why nothing had needed
    this until a check went through the real hook path.
    """
    class _Constants(object):
        ARENA_BONUS_TYPE = Bonus

    sys.modules.setdefault('constants', _Constants())


def _hooked(service, queue=None):
    from unicum.battle_reports import BattleReports
    from unicum.runtime.session import Session

    class _Reports(BattleReports):
        def _service(self):
            return service

    return _Reports(Session(0), Settings(True), queue=queue, link=Link())


def _queue():
    from unicum.report_queue import Queue

    return Queue(store=os.path.join(tempfile.mkdtemp(), 'q.json'))


def check_battle_report_hooks():
    _constants()

    both = _Both()
    queue = _queue()
    _hooked(both, queue).install()
    check('the event is attached when the service has one', len(both.onResultPosted.handlers) == 1)

    both.onResultPosted.fire(results())
    check('a battle arriving by the event is captured', len(queue.all()) == 1)

    answer = both.postResult(results(arenaUniqueID=12457893456789012346))
    check('a battle arriving by the patch is captured as well', len(queue.all()) == 2)
    # The client's own call runs first and its answer is handed back untouched:
    # a capture must never be the reason a player does not see their results.
    check("and the client's own answer is returned", answer == 'the client answer')

    both.onResultPosted.fire(results())
    check('a battle reaching the capture twice is queued once', len(queue.all()) == 2)

    alone = _EventOnly()
    _hooked(alone).install()
    check('an event with no postResult beside it is still attached',
          len(alone.onResultPosted.handlers) == 1)

    empty = _queue()
    _hooked(_Neither(), empty).install()
    check('a service offering no way in captures nothing and does not raise',
          not empty.all())
