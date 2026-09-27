"""Battle results captured as they arrive, for the destinations that want them.

Wargaming's public API answers lifetime and recent statistics for random
battles, and has stopped refreshing some of the other modes: ranked has been
frozen there for weeks. A site scoring a ranked season from that API reads the
same numbers every day. The results themselves are not gone, they are in the
client, which is where this reads them.

Why arrival, and not the results screen
---------------------------------------
`battle_results.py` decorates the post-battle view, and its hook runs when the
view is built. That is the right place to draw in, and the wrong place to count
from: a player who queues straight into the next battle never opens the screen,
and that battle would simply be missing. Worse, it would be missing quietly --
a rating computed from nine battles out of ten looks like a rating, not like a
fault. What is drawn may be missed; what is counted may not. So this sits on
the results reaching the client, which happens whether anyone looks or not.

What leaves the client
----------------------
Post-battle numbers only, never anything about a battle in progress. That is
what keeps this on the right side of Wargaming's fair play rules, and it is a
property of where the hook sits rather than a promise in a document: at the
moment this runs, the battle is over.

Where a capture goes
--------------------
Straight to disk, in `report_queue.py`: a battle that fails to send cannot be
played again, and the client can be closed between two battles or be offline
for a whole session. Sending reads from that queue, never from here.
"""
import logging
import time

from unicum import modes
from unicum.report_queue import Queue

_logger = logging.getLogger('unicum.battle_reports')

# The seven counters that travel per battle, and the key each one is read from
# in the client's own results. The client's spelling on the left is not ours:
# `kills` is `frags` everywhere a rating is computed.
METRICS = (
    ('xp', 'xp'),
    ('damage_dealt', 'damageDealt'),
    ('damage_received', 'damageReceived'),
    ('frags', 'kills'),
    ('spotted', 'spotted'),
    ('capture_points', 'capturePoints'),
    ('dropped_capture_points', 'droppedCapturePoints'),
)

# `personal` is keyed by vehicle, with one entry that is not a vehicle.
_NOT_A_VEHICLE = ('avatar', )

# What the client puts in `deathReason` for a vehicle that came out alive.
_ALIVE = -1


def own_vehicles(personal):
    """The player's own vehicles in a battle's results.

    A battle holds more than one when the mode lets a player respawn, so
    nothing here assumes a single vehicle: the counters are summed over all of
    them, which is also how Wargaming counts a Frontline battle.
    """
    if not isinstance(personal, dict):
        return []
    return [value for key, value in personal.items()
            if key not in _NOT_A_VEHICLE and isinstance(value, dict)]


def metrics_of(vehicles):
    """{our name: value} for the seven counters, summed over the vehicles.

    A counter the client does not report reads as zero rather than being left
    out: a destination validating the report would reject an incomplete one,
    and a missing counter is indistinguishable from an idle player anyway.
    """
    out = {}
    for ours, theirs in METRICS:
        total = 0
        for vehicle in vehicles:
            value = vehicle.get(theirs)
            # Booleans are ints in Python and would count as 1. Nothing in
            # these results is a bool today, and a client that changed its
            # mind about one would corrupt a counter rather than skip it.
            if isinstance(value, bool) or not isinstance(value, (int, long, float)):
                continue
            total += int(value)
        out[ours] = max(total, 0)
    return out


def survived(vehicles):
    """Whether the player came out of the battle alive.

    Alive means no vehicle of theirs died, which is what Wargaming's own
    `survived_battles` counts. With nothing to read, the answer is False: a
    survival wrongly claimed is worth score the player did not earn, where one
    wrongly denied only costs them.
    """
    if not vehicles:
        return False
    for vehicle in vehicles:
        if vehicle.get('deathReason', _ALIVE) != _ALIVE:
            return False
    return True


def outcome_of(winner_team, own_team):
    """'win', 'loss' or 'draw' from the winning team and the player's own.

    Team 0 is the client's way of saying nobody won. Returns None when either
    team is unreadable: a battle whose result we would have to guess is not
    captured at all, rather than counted as a loss.
    """
    if not isinstance(winner_team, (int, long)) or isinstance(winner_team, bool):
        return None
    if not isinstance(own_team, (int, long)) or isinstance(own_team, bool):
        return None
    if winner_team == 0:
        return 'draw'
    return 'win' if winner_team == own_team else 'loss'


def finished_at(common, now=None):
    """The battle's end as `YYYY-MM-DDTHH:MM:SSZ`, in UTC.

    Built from the arena's creation and its duration, which is when the battle
    actually ended -- not when this ran. The two differ by however long the
    results took to arrive, and by everything a queued report waits on disk.

    Falls back to now when the client gives neither, which is wrong by seconds
    and never by days; a destination refusing a future timestamp would reject
    the report, so nothing here is allowed to drift forward.
    """
    created = common.get('arenaCreateTime') if isinstance(common, dict) else None
    duration = common.get('duration') if isinstance(common, dict) else None
    stamp = None
    if isinstance(created, (int, long, float)) and not isinstance(created, bool):
        stamp = float(created)
        if isinstance(duration, (int, long, float)) and not isinstance(duration, bool):
            stamp += float(duration)
    if stamp is None:
        stamp = time.time() if now is None else now
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stamp))


def arena_id_of(common):
    """`arenaUniqueID` as a string of decimal digits, or None.

    A string because it is an unsigned 64-bit value: sent as a JSON number it
    is already damaged, most parsers falling back to a float beyond 2^53 and
    dropping the low-order digits without a word. Python 2 holds it exactly as
    a long, so the only place it can be lost is on the wire.
    """
    if not isinstance(common, dict):
        return None
    value = common.get('arenaUniqueID')
    if isinstance(value, bool) or not isinstance(value, (int, long)):
        return None
    if value <= 0:
        return None
    return str(value)


def report_of(results, mode=None, now=None, constants=None):
    """One battle's report, in the shape a destination is given, or None.

    None whenever the results are not a battle this can honestly describe: no
    arena id, no readable outcome, no vehicle of the player's own. A report
    that had to be guessed at is worse than a missing one, because it counts.

    `constants` is passed through to `modes.mode_of`, as its own callers do, so
    the mode a battle is read as can be checked outside a client.
    """
    if not isinstance(results, dict):
        return None
    common = results.get('common')
    arena_id = arena_id_of(common)
    if arena_id is None:
        return None
    vehicles = own_vehicles(results.get('personal'))
    if not vehicles:
        return None
    own_team = vehicles[0].get('team')
    outcome = outcome_of(common.get('winnerTeam'), own_team)
    if outcome is None:
        return None
    if mode is None:
        mode = modes.mode_of(common.get('bonusType'), constants)
    return {
        'arena_unique_id': arena_id,
        'mode': mode,
        'finished_at': finished_at(common, now),
        'outcome': outcome,
        'survived': survived(vehicles),
        'metrics': metrics_of(vehicles),
    }


class BattleReports(object):
    """Captures a battle's results the moment the client has them."""

    def __init__(self, session, settings, queue=None, destinations=None):
        self._session = session
        self._settings = settings
        self._queue = queue if queue is not None else Queue()
        self._destinations = destinations

    def install(self):
        service = self._service()
        if service is None:
            _logger.info('no battle results service in this client, nothing captured')
            return
        # The service's own event, when it has one: it fires as the results are
        # posted, which is the arrival this whole module is about. Patching
        # `postResult` reaches the same moment and is the fallback, because an
        # event that does not exist cannot be subscribed to and a client that
        # renamed it must not take the capture down with it.
        event = getattr(service, 'onResultPosted', None)
        if event is not None and hasattr(event, '__iadd__'):
            self._session.subscribe(event, self._on_posted)
            _logger.info('capturing battle results from onResultPosted')
            return
        holder = type(service)
        if hasattr(holder, 'postResult'):
            self._session.patch(holder, 'postResult', self._wrap_post)
            _logger.info('capturing battle results by way of %s.postResult', holder.__name__)
            return
        _logger.warning('found no way to follow battle results arriving; nothing is captured')

    @staticmethod
    def _service():
        try:
            from helpers import dependency
            from skeletons.gui.battle_results import IBattleResultsService
            return dependency.instance(IBattleResultsService)
        except Exception:
            _logger.debug('could not reach the battle results service', exc_info=True)
            return None

    def _wrap_post(self, original):

        def postResult(service, result, *args, **kwargs):
            # The client's own call first: a capture that raised must never be
            # the reason a player does not see their results.
            outcome = original(service, result, *args, **kwargs)
            self._on_posted(result)
            return outcome

        return postResult

    def _on_posted(self, posted=None, *args):
        try:
            self.capture(raw_results(posted, on_miss=_describe))
        except Exception:
            _logger.exception('could not capture a battle')

    def wanted(self, mode):
        """Whether anything is waiting for a battle of this mode.

        unicum.gg takes them all while the player leaves the switch on. An extra
        destination takes only the modes it asked for, so a site scoring ranked
        never receives a random battle.
        """
        if self._settings.sends_battle_reports():
            return True
        if self._destinations is None:
            return False
        return bool(self._destinations.wanting(mode))

    def capture(self, results, constants=None):
        """Queue a battle's results, and say whether anything was queued."""
        if results is None:
            return False
        report = report_of(results, constants=constants)
        if report is None:
            _logger.debug('a battle arrived that could not be described, skipped')
            return False
        # Asked after the report is built, because the answer depends on the
        # mode, and cheap enough: a battle nobody is waiting for is not kept.
        # Keeping it "just in case" would be a copy of the player's play history
        # sitting on their disk for no one.
        if not self.wanted(report['mode']):
            return False
        if not self._queue.add(report):
            return False
        _logger.info('captured a %s battle, %d queued', report['mode'], len(self._queue.all()))
        return True


# Where the server's own results dict has been found hanging off the reusable
# view the service passes round. Ordered: the first that yields a dict carrying
# `common` wins. A client that moves it adds a path here rather than anywhere
# else, which is the reason this list exists at all.
_RAW_PATHS = (
    ('_ReusableInfo__personal', '_PersonalInfo__personal'),
    ('personal', '_PersonalInfo__personal'),
    ('personal', '_personal'),
    ('_personal', ),
    ('personal', ),
)


def raw_results(posted, on_miss=None):
    """The client's own results dict, out of whatever the hook was handed.

    The service passes round a reusable *view* of the results, not the dict the
    server sent. That view's shape is the client's business and moves with it;
    the dict underneath does not, because it is the server's payload. So the
    dict is what every function here reads, and this is the only place that has
    to know where the client keeps it.

    A dict goes straight through, which is what the checks hand it.

    When no path leads to one, `on_miss` is called with what we did get. That
    is not politeness: this is the one piece of the module that cannot be
    verified outside a running client, so a miss has to describe itself in
    `game.log` well enough to be fixed from one session's log -- guessing again
    from a bug report costs a round trip through someone else's evening.
    """
    if isinstance(posted, dict):
        return posted
    for path in _RAW_PATHS:
        value = posted
        for step in path:
            value = getattr(value, step, None)
            if value is None:
                break
        if isinstance(value, dict) and 'common' in value:
            return value
    if on_miss is not None:
        on_miss(posted)
    return None


def _describe(posted):
    """What we were handed, for a log line that makes a missing path fixable."""
    try:
        names = [name for name in dir(posted) if 'personal' in name.lower()]
        _logger.warning('battle results arrived as %s; no known path to the results dict. '
                        'Attributes mentioning "personal": %s',
                        type(posted).__name__, ', '.join(names) or 'none')
    except Exception:
        _logger.warning('battle results arrived in an unreadable shape')


def install(session, settings, destinations=None):
    reports = BattleReports(session, settings, destinations=destinations)
    reports.install()
    return reports
