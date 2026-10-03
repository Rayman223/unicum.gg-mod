"""Checks for asking the server about a battle nobody opened.

This is the half the capture could not do. A real client kept every battle's
numbers to itself until the player opened the results screen, so what is
checked here is the ask: hearing that a battle finished, asking about one
battle at a time, and surviving a server that refuses or never answers.

The failures worth having checks are the quiet ones. A queue that jams on an
answer that never came, or a battle asked about every twenty seconds for the
rest of the session, both look like nothing at all from the garage.
"""
import os
import sys
import tempfile

from checks.battle_fixtures import Bonus, Link, Settings, results
from checks.common import check


class _Event(object):
    """A WG Event, reduced to attaching and firing.

    Local rather than shared: `checks.engine.FakeEvent` cannot fire, and what
    these checks need is the firing.
    """

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class _Message(object):
    """A service channel message, as the client's own wrapper carries one."""

    def __init__(self, type=None, data=None):
        self.type = type
        self.data = data


class _Session(object):
    """Enough session to attach to an event and hold a repeating tick."""

    def __init__(self):
        self.subscriptions = []
        self.repeats = []

    def subscribe(self, event, handler):
        event += handler
        self.subscriptions.append((event, handler))
        return handler

    def repeat(self, interval, func):
        self.repeats.append((interval, func))

    def on_close(self, func):
        pass


class _Cache(object):
    """The client's battle results cache: remembers what it was asked."""

    def __init__(self, answers=None):
        self.asked = []
        self._answers = dict(answers or {})
        self._pending = {}

    def get(self, arena_id, callback):
        self.asked.append(arena_id)
        answer = self._answers.get(arena_id)
        if answer is None:
            # Asked and left hanging, which is what a lost callback looks like.
            self._pending[arena_id] = callback
            return
        code, payload = answer
        callback(code, payload)

    def answer_now(self, arena_id, code, payload=None):
        self._pending.pop(arena_id)(code, payload)


class _Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _requests(cache, garage=True, deliver=None, limit=None, clock=None, wanted=None):
    from unicum.results_request import LIMIT, Requests

    taken = [] if deliver is None else deliver
    requests = Requests(_Session(), lambda results, source: taken.append((results, source)),
                        wanted=wanted, garage=lambda: garage, cache=lambda: cache,
                        limit=LIMIT if limit is None else limit,
                        now=clock or _Clock())
    return requests, taken


def _constants():
    """Give the fake client the bonus types the capture reads."""
    class _Constants(object):
        ARENA_BONUS_TYPE = Bonus

    sys.modules.setdefault('constants', _Constants())


def check_results_request_filter():
    """Only a finished battle is asked about, and only when it names one.

    Everything the server says arrives on the same event: rewards, reboots,
    clan news. Without the filter the mod would ask the server about the arena
    id of a server reboot, once per message, for the whole session.
    """
    from unicum.results_request import arena_id_of, arrival_type

    battle = arrival_type()
    check('the message type is a number this client owns', isinstance(battle, int))

    arena = arena_id_of(_Message(battle, {'arenaUniqueID': 4455}), wanted=battle)
    check('a finished battle gives up its arena', arena == 4455)

    other = arena_id_of(_Message(battle + 7, {'arenaUniqueID': 4455}), wanted=battle)
    check('another kind of server message is ignored', other is None)

    check('a battle message carrying no arena is ignored',
          arena_id_of(_Message(battle, {'guiType': 1}), wanted=battle) is None)
    check('a message carrying no data at all is ignored',
          arena_id_of(_Message(battle, None), wanted=battle) is None)
    # The id is a 64-bit number in a dict the server filled: a string there
    # would otherwise be asked about and refused, spending every attempt.
    check('an arena that is not a number is ignored',
          arena_id_of(_Message(battle, {'arenaUniqueID': 'soon'}), wanted=battle) is None)


def check_results_request_only_wanted_modes():
    """A battle nothing is waiting for is not asked about at all.

    The announcement carries the arena's bonus type, so the mod can tell a
    random battle from a ranked one before spending a request. A player whose
    only destination scores ranked plays mostly random: without this, every
    one of those evenings is a request per battle for numbers thrown away on
    arrival.
    """
    _constants()
    from unicum.results_request import announced_mode, arrival_type

    battle = arrival_type()
    ranked = _Message(battle, {'arenaUniqueID': 81, 'bonusType': Bonus.RANKED})
    random = _Message(battle, {'arenaUniqueID': 82, 'bonusType': Bonus.REGULAR})

    check('an announced ranked battle is named', announced_mode(ranked) == 'ranked')
    check('an announced random battle is named', announced_mode(random) == 'random')
    # Fail open: not asking about a wanted battle costs the battle, while
    # asking about an unwanted one costs one request.
    check('an announcement that does not say is left unnamed',
          announced_mode(_Message(battle, {'arenaUniqueID': 83})) is None)

    cache = _Cache({81: (1, results()), 82: (1, results())})
    requests, _ = _requests(cache, wanted=lambda mode: mode == 'ranked')
    requests._announced(1, ranked)
    requests._announced(2, random)
    check('only the wanted mode is asked about', cache.asked == [81])

    unnamed = _Message(battle, {'arenaUniqueID': 83})
    cache._answers[83] = (1, results())
    requests._announced(3, unnamed)
    check('a battle whose mode is unknown is asked about anyway', cache.asked == [81, 83])


def check_results_request_queue():
    """Battles queue once each, oldest first, and the queue is bounded."""
    requests, _ = _requests(_Cache(), garage=False, limit=3)

    check('a battle is remembered', requests.remember(1) is True)
    # The client announces a battle again when it is opened from the
    # notification centre, so without this the same battle is asked about
    # twice and the second answer is thrown away by the queue downstream.
    check('the same battle is not remembered twice', requests.remember(1) is False)
    requests.remember(2)
    requests.remember(3)
    check('they wait in the order they arrived', requests.waiting() == [1, 2, 3])

    requests.remember(4)
    # A player who queues straight into battle all evening: the queue has to
    # stop somewhere, and the oldest battle is the one already lost to the
    # client's own cache being cleared.
    check('the oldest goes when the queue is full', requests.waiting() == [2, 3, 4])


def check_results_request_one_at_a_time():
    """One request in flight, because a second is answered with a cooldown.

    The client's cache serves one at a time. Asking about a backlog all at
    once would have every battle after the first refused, and each refusal
    spends one of the few attempts a battle gets.
    """
    cache = _Cache()
    requests, _ = _requests(cache)

    requests.remember(11)
    requests.remember(12)
    requests.drain()
    check('only the first battle is asked about', cache.asked == [11])

    requests.drain()
    check('draining again asks for nothing while one is in flight', cache.asked == [11])

    cache.answer_now(11, 1, results())
    check('the next is asked about once the first is answered', cache.asked == [11, 12])


def check_results_request_waits_for_the_garage():
    """Nothing is asked for outside the garage.

    The cache refuses every request when the account is not the player, and
    the refusal is indistinguishable from a battle the server will not serve.
    """
    cache = _Cache()
    requests, _ = _requests(cache, garage=False)

    requests.remember(21)
    requests.drain()
    check('a battle is not asked about in a battle', cache.asked == [])
    check('and it is still waiting', requests.waiting() == [21])


def check_results_request_delivers():
    """An answered battle reaches the capture, and lands in the queue.

    End to end through the real capture rather than a stand-in for it, because
    the point of the ask is the report at the other end: the answer arrives in
    the shape the client posts, and the only thing proving that is a report.
    """
    _constants()
    from unicum.battle_reports import BattleReports
    from unicum.report_queue import Queue
    from unicum.results_request import Requests

    queue = Queue(store=os.path.join(tempfile.mkdtemp(), 'q.json'))
    reports = BattleReports(_Session(), Settings(True), queue=queue, link=Link())
    cache = _Cache({31: (1, results())})
    requests = Requests(_Session(), reports.arrived, garage=lambda: True,
                        cache=lambda: cache, now=_Clock())

    requests.remember(31)
    requests.drain()
    check('the answered battle is captured', len(queue.all()) == 1)
    check('and nothing is left waiting', requests.waiting() == [])

    stored = queue.all()[0]
    check('the report carries the battle it was asked about', stored['mode'] == 'ranked')


def check_results_request_refusals():
    """A refusal that means "later" is retried; one that means "never" is not."""
    from unicum.results_request import retryable

    # -5 is RES_COOLDOWN, -1 is RES_FAILURE. Read from the client when it is
    # there, so the numbers here are the fallback being checked.
    check('a cooldown is worth asking again for', retryable(-5) is True)
    check('so is an account not yet in the garage', retryable(-3) is True)
    check('a plain failure is not', retryable(-1) is False)
    check('neither is a nonsense code', retryable(None) is False)

    cache = _Cache({41: (-5, None), 42: (-1, None)})
    requests, _ = _requests(cache)

    requests.remember(41)
    requests.drain()
    check('a battle refused with a cooldown waits again', requests.waiting() == [41])

    requests.remember(42)
    cache._answers[41] = (1, results())
    requests.drain()
    check('it is asked about again', cache.asked == [41, 41, 42])
    check('a battle refused for good is dropped', requests.waiting() == [])


def check_results_request_gives_up():
    """A battle refused again and again is eventually let go of.

    Without a limit, a refusal the server will keep making turns into a
    request every tick for as long as the client runs.
    """
    cache = _Cache({51: (-5, None)})
    requests, _ = _requests(cache)

    requests.remember(51)
    for _ in range(8):
        requests.drain()

    check('it is asked about a bounded number of times', len(cache.asked) == 5)
    check('and then let go of', requests.waiting() == [])


def check_results_request_stale():
    """An answer that never comes does not hold every later battle shut.

    The worst failure available here: silent, and it grows. One callback the
    client never runs would otherwise stop the mod asking about anything for
    the rest of the session.
    """
    clock = _Clock()
    cache = _Cache({61: None, 62: (1, results())})
    requests, taken = _requests(cache, clock=clock)

    requests.remember(61)
    requests.remember(62)
    requests.drain()
    check('the first battle is asked about and left hanging', cache.asked == [61])

    requests.drain()
    check('the next waits while the answer might still come', cache.asked == [61])

    clock.now += 61.0
    requests.drain()
    check('once the answer is late, the next battle is asked about', 62 in cache.asked)
    check('and it was answered', len(taken) == 1)

    # The late answer is still an answer: the battle was played, and the queue
    # downstream keeps one report per arena anyway.
    asked_before = len(cache.asked)
    cache.answer_now(61, 1, results(arenaUniqueID=61))
    check('and a late answer is still taken', len(taken) == 2)
    # Answered is answered: a battle let go of as stale and then answered must
    # not be queued for a second request whose answer is already in hand.
    check('it is not asked about again afterwards', len(cache.asked) == asked_before)
    check('and nothing is left waiting', requests.waiting() == [])


def check_results_request_install():
    """The mod attaches to the public event, or says it captured nothing.

    The private method every other mod patches is name-mangled and cannot be
    wrapped by two mods safely; the event is handed to subscribers by name.
    """
    from unicum.results_request import Requests, arrival_type

    event = _Event()
    channel = type('ServiceChannel', (object,), {'onChatMessageReceived': event})()
    events = type('MessengerEvents', (object,), {'serviceChannel': channel})()
    module = type('Module', (object,), {'g_messengerEvents': events})
    standing = sys.modules.get('messenger.proto.events')
    sys.modules['messenger.proto.events'] = module
    try:
        cache = _Cache({71: (1, results())})
        session = _Session()
        requests = Requests(session, lambda results, source: None,
                            garage=lambda: True, cache=lambda: cache, now=_Clock())
        check('attaching to the service channel succeeds', requests.install() is True)
        check('it is attached to the public event', len(event.handlers) == 1)
        # A tick as well as the event: the first announcement can arrive while
        # the player is still in the battle, where nothing can be asked.
        check('and a tick retries what could not be asked then', len(session.repeats) == 1)

        event.fire(7, _Message(arrival_type(), {'arenaUniqueID': 71}))
        check('an announced battle is asked about', cache.asked == [71])

        event.fire(8, _Message(arrival_type() + 7, {'arenaUniqueID': 72}))
        check('another kind of message asks for nothing', cache.asked == [71])
    finally:
        if standing is None:
            sys.modules.pop('messenger.proto.events', None)
        else:
            sys.modules['messenger.proto.events'] = standing


def check_results_request_no_channel():
    """A client with no service channel is said to capture less, not crashed."""
    from unicum.results_request import Requests

    standing = sys.modules.get('messenger.proto.events')
    sys.modules.pop('messenger.proto.events', None)
    sys.modules['messenger.proto.events'] = type('Module', (object,), {})
    try:
        requests = Requests(_Session(), lambda results, source: None,
                            garage=lambda: True, cache=lambda: _Cache(), now=_Clock())
        check('installing stands down instead of raising', requests.install() is False)
    finally:
        if standing is None:
            sys.modules.pop('messenger.proto.events', None)
        else:
            sys.modules['messenger.proto.events'] = standing
