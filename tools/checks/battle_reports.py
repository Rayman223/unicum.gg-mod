"""Checks for the battles the mod reports: what is read, and what is never counted twice.

Everything here works on the server's own results dict, which is what
battle_reports reads. The one part that cannot be checked outside a running
client is finding that dict on the object the service passes round; a miss
there says so in game.log, with the attribute names it did find.
"""

import os
import tempfile

from checks.common import check


class Bonus(object):
    """The arena bonus types the mod names, as this client numbers them."""

    REGULAR = 1
    RANKED = 22
    COMP7 = 43
    COMP7_LIGHT = 49
    SORTIE_2 = 20
    EPIC_BATTLE = 27
    TRAINING = 2


def _results(**overrides):
    """A ranked win, one vehicle, as the client's results dict."""
    common = {'arenaUniqueID': 12457893456789012345,
              'winnerTeam': 1,
              'bonusType': 22,
              'arenaCreateTime': 1790359200,
              'duration': 600}
    common.update(overrides.pop('common', {}))
    vehicle = {'team': 1,
               'deathReason': -1,
               'xp': 1420,
               'damageDealt': 3120,
               'damageReceived': 1880,
               'kills': 3,
               'spotted': 2,
               'capturePoints': 0,
               'droppedCapturePoints': 40}
    vehicle.update(overrides.pop('vehicle', {}))
    personal = {'8721': vehicle, 'avatar': {'team': 1}}
    personal.update(overrides.pop('personal', {}))
    return {'common': common, 'personal': personal}


def check_battle_report_reading():
    from unicum.battle_reports import metrics_of, outcome_of, own_vehicles, report_of, survived

    check('the avatar entry is not taken for a vehicle',
          len(own_vehicles(_results()['personal'])) == 1)

    metrics = metrics_of(own_vehicles(_results()['personal']))
    check('the client\'s kills are read as frags', metrics['frags'] == 3)
    check('damage is read as it stands', metrics['damage_dealt'] == 3120)
    check('all seven counters are always present', len(metrics) == 7)
    check('a counter the client omits reads as zero',
          metrics_of([{'kills': 1}])['damage_dealt'] == 0)
    # A respawn mode gives a player several vehicles in one battle, and their
    # battle is the sum of them, which is how Wargaming counts it too.
    check('two vehicles in one battle are summed',
          metrics_of([{'kills': 1}, {'kills': 2}])['frags'] == 3)
    # Booleans are ints in Python: one counted as 1 would quietly inflate a score.
    check('a boolean is not counted as one',
          metrics_of([{'kills': True}])['frags'] == 0)

    check('the winning team means a win', outcome_of(1, 1) == 'win')
    check('the other team winning means a loss', outcome_of(2, 1) == 'loss')
    check('nobody winning means a draw', outcome_of(0, 1) == 'draw')
    # Not a loss: a battle whose outcome we would have to guess is not reported.
    check('an unreadable outcome is no outcome', outcome_of(None, 1) is None)
    check('an unreadable team is no outcome either', outcome_of(1, None) is None)

    check('a vehicle that came out alive survived', survived([{'deathReason': -1}]))
    check('a vehicle that died did not', not survived([{'deathReason': 0}]))
    check('one death in a battle ends its survival',
          not survived([{'deathReason': -1}, {'deathReason': 2}]))
    # False rather than True: a survival wrongly claimed is score the player
    # never earned, where one wrongly denied only costs them.
    check('nothing to read is not a survival', not survived([]))

    report = report_of(_results(), constants=Bonus)
    check('a ranked battle is read as ranked', report['mode'] == 'ranked')
    # The modes are told apart by bonus type, so a Stronghold battle is never
    # reported as the ranked one a tournament would score.
    check('a stronghold battle is not read as ranked',
          report_of(_results(common={'bonusType': Bonus.SORTIE_2}), constants=Bonus)['mode'] == 'stronghold')
    check('the battle is timed by its end, not by when it was read',
          report['finished_at'] == '2026-09-25T18:10:00Z')
    check('the outcome rides along', report['outcome'] == 'win')
    check('so does the survival', report['survived'] is True)


def check_battle_report_arena_id():
    from unicum.battle_reports import arena_id_of, report_of

    # The whole reason this is a string: 2^64-1 sent as a JSON number comes back
    # a float, and the low-order digits are gone without a word.
    huge = 18446744073709551615
    check('an arena id is a string of digits', arena_id_of({'arenaUniqueID': huge}) == str(huge))
    check('and it keeps every digit',
          arena_id_of({'arenaUniqueID': huge}) == '18446744073709551615')
    check('a missing arena id is None', arena_id_of({}) is None)
    check('a zero arena id is None', arena_id_of({'arenaUniqueID': 0}) is None)
    check('a boolean arena id is None', arena_id_of({'arenaUniqueID': True}) is None)

    check('a battle without an arena id is not reported',
          report_of(_results(common={'arenaUniqueID': None}), constants=Bonus) is None)
    check('a battle with no vehicle of ours is not reported',
          report_of({'common': {'arenaUniqueID': 1}, 'personal': {'avatar': {}}}) is None)
    check('a battle whose outcome is unreadable is not reported',
          report_of(_results(common={'winnerTeam': None}), constants=Bonus) is None)
    check('something that is not results at all is not reported', report_of(None) is None)


def check_battle_report_queue():
    from unicum.battle_reports import report_of
    from unicum.report_queue import Queue, deduplicate, trim

    first = {'arena_unique_id': '1'}
    second = {'arena_unique_id': '2'}
    check('a repeated arena is kept once',
          deduplicate([first, second, dict(first)]) == [first, second])
    # The client posts a battle's results again when an older one is opened from
    # the notification centre, and a counter that added it twice would credit
    # score nobody earned.
    check('the first of two reports of one arena is the one kept',
          deduplicate([{'arena_unique_id': '1', 'metrics': {'frags': 3}},
                       {'arena_unique_id': '1', 'metrics': {'frags': 9}}])[0]['metrics']['frags'] == 3)

    # The OLDEST go: a destination scoring this week's tournament wants this
    # week's battles, and a queue that refused new ones would stop capturing.
    reports = [{'arena_unique_id': str(index)} for index in range(5)]
    check('the queue drops its oldest when full',
          [r['arena_unique_id'] for r in trim(reports, 3)] == ['2', '3', '4'])
    check('a queue under the limit is untouched', trim(reports, 10) == reports)

    store = os.path.join(tempfile.mkdtemp(), 'battle-reports.json')
    queue = Queue(store=store)
    check('a fresh queue is empty', queue.all() == [])
    check('a captured battle is queued', queue.add(report_of(_results(), constants=Bonus)))
    check('the same battle is not queued twice', not queue.add(report_of(_results(), constants=Bonus)))
    check('and the queue still holds the one', len(queue.all()) == 1)

    # The point of the file: a battle that failed to send cannot be played again.
    check('the queue survives the client being closed', Queue(store=store).all() == queue.all())

    queue.drop(['12457893456789012345'])
    check('a report taken by a destination leaves the queue', queue.all() == [])
    check('and it is gone from the file too', Queue(store=store).all() == [])

    # A client killed mid-write leaves a truncated file. The battles in it are
    # gone either way; keeping it would only stop every capture after it.
    with open(store, 'wb') as handle:
        handle.write('{"schema": 1, "reports": [{"arena')
    check('an unreadable queue file starts a new queue', Queue(store=store).all() == [])


def check_battle_report_setting():
    from unicum.runtime.session import Session
    from unicum.settings import Settings, validate

    check('battles are reported unless the player says otherwise',
          validate({})['sendBattleResults'] is True)
    check('the switch survives a round trip',
          validate({'sendBattleResults': False})['sendBattleResults'] is False)
    settings = Settings(Session(generation=0),
                        store=os.path.join(tempfile.mkdtemp(), 'settings.json'))
    check('a fresh install reports them', settings.sends_battle_reports())
    settings.update({'sendBattleResults': False})
    check('turning it off stops them', not settings.sends_battle_reports())
    settings.update({'sendBattleResults': True, 'enabled': False})
    check('the mod off reports nothing either', not settings.sends_battle_reports())


def check_battle_report_capture():
    from unicum.battle_reports import BattleReports
    from unicum.report_queue import Queue

    class _Settings(object):
        def __init__(self, on):
            self._on = on

        def sends_battle_reports(self):
            return self._on

    store = os.path.join(tempfile.mkdtemp(), 'battle-reports.json')
    reports = BattleReports(None, _Settings(True), queue=Queue(store=store))
    check('a battle that arrives is captured', reports.capture(_results(), constants=Bonus))
    check('the same battle arriving again is not', not reports.capture(_results(), constants=Bonus))

    off = BattleReports(None, _Settings(False), queue=Queue(store=os.path.join(tempfile.mkdtemp(), 'q.json')))
    check('nothing is captured while the switch is off', not off.capture(_results(), constants=Bonus))


def check_battle_report_raw_lookup():
    from unicum.battle_reports import raw_results

    payload = _results()

    class _Personal(object):
        def __init__(self):
            self._PersonalInfo__personal = payload

    class _Reusable(object):
        def __init__(self):
            self.personal = _Personal()

    check('a results dict goes straight through', raw_results(payload) is payload)
    check('the dict is found under the reusable view', raw_results(_Reusable()) is payload)

    missed = []
    check('an unknown shape yields nothing',
          raw_results(object(), on_miss=missed.append) is None)
    # Not silence: this is the one piece that cannot be checked against a real
    # client, so a miss has to describe itself well enough to fix from one log.
    check('and it reports what it was handed', len(missed) == 1)
