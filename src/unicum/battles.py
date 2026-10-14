"""The battles this client played, sent to unicum.gg.

Wargaming's API publishes an account's running totals and nothing whatever
about a single battle: not who was in it, not what each of them did, not which
map or which cluster. So a battle on unicum.gg exists only because somebody
running this mod was in it, and this module is the whole of that path.

Where the numbers come from
---------------------------
The client's own battle results, handed to us by the game. Not a .wotreplay:
recording is a player setting, and new accounts do not get it at its highest,
so reading files would quietly collect from a subset of players and call it the
playerbase. The results arrive in every client, for every battle, whatever that
setting says.

Read by patching `BattleResultsService.postResult`, which receives the raw
dictionary the server sent. **Not `onResultPosted`**, which was the first
attempt: that event hands over a stats controller, and only the Gameface one
(`RandomBattleResultStatsCtrl`) implements `getResults`. Every Flash mode --
Ranked, CyberSport, Stronghold, Maps Training, and the fallback any bonus type
we have not met lands on -- inherits `IBattleResultStatsCtrl.getResults`, which
returns None. Those modes would have reported nothing at all, and said so in
the log as if the extractor had refused them. `postResult` is upstream of every
composer, so it is the one place that sees all of them.

The raw dictionary is the same structure a replay carries in its second block,
which is how the extractor below could be measured against 1913 real battles
before it ever ran in the client. What the dictionary does NOT carry is the
map's name, the gameplay and the client version: those live in a replay's meta
block, not in the results, so they are read from the client here and passed in.

What one battle costs
---------------------
Measured over 1913 real battles: 10.2 KB of JSON for a thirty-vehicle random
battle, 4.9 KB for a fourteen-vehicle skirmish, 20.8 KB for a sixty-vehicle
Frontline. Against a 1293 KB replay of the same battle, which is the reason
this is what gets sent rather than the file.

One battle, any sender
----------------------
A battle names all thirty of its players, so it reaches the server from
whichever participant happens to run the mod, and the id it is keyed by is a
deduplication key rather than an ownership one: the second sender of a battle
is answered "already known" rather than refused. Coverage grows by whole teams.

When it sends, and what it is allowed to forget
-----------------------------------------------
Appended to a file the moment the results land, sent from the garage. Appended
rather than rewritten because this runs on the thread that draws the game: one
battle is a 10 KB line, where rewriting a full queue was 44 ms and three
dropped frames, landing exactly on the results screen's animation.

**A battle leaves the queue only when the server names it.** The answer lists
ids, never counts, and that is load-bearing: the queue is the only copy of a
battle not yet stored, and a count cannot be acted on, so a client reading
counts had no choice but to drop the whole batch on any answer it could not
make sense of -- including an HTML error page served with a 200.
"""
import json
import logging
import os
import re
import time

from unicum import config

_logger = logging.getLogger('unicum.battles')

# Where battles wait until the server has taken them, one JSON object a line.
#
# A line a battle, because this file is appended to from the battle results
# handler, on the thread that draws the game. The whole-file rewrite only
# happens once a flush has been answered, which is at the garage.
QUEUE = os.path.join('mods', 'configs', 'unicum', 'battles.ndjson')

# The ids the server has already answered for, so a battle whose results the
# player reopens from the notification centre is not offered again.
#
# `postResult` runs again for a battle read out of the client's own cache, so
# without this every stroll through the notification centre reposted up to
# twenty battles. This is the same job `loadouts.py` gives its fingerprint
# store, and leaving it out was the half of that pattern this module missed.
SETTLED = os.path.join('mods', 'configs', 'unicum', 'battles-sent.json')

# How many settled ids to remember. A fortnight of heavy play, at 20 bytes an
# id, so the file stays a few kilobytes.
_MAX_SETTLED = 500

# The battles this client was in, until each one has been written down.
#
# **The client only posts a battle's results when it SHOWS them.** A player who
# returns to the garage and queues straight back up without the results window
# opening leaves a battle that, as far as this module goes, never happened: no
# `postResult`, so no `_record`, so no line in the queue and nothing in the log
# either. Two of nine battles went that way in one afternoon, and the only
# trace of them was `motion`'s own "sampling battle" line.
#
# So the arena id is written down when the battle STARTS, off the same event
# `motion` uses, and the results are asked for at the garage rather than waited
# for. The file is the record of what is still owed.
PLAYED = os.path.join('mods', 'configs', 'unicum', 'battles-played.json')

# How many battles to ask the server about per garage visit.
#
# One. Each ask is a real request for a real battle's results, and a player who
# has just installed the mod with twenty unreported battles behind them should
# not open the garage into twenty of them. The next garage takes the next one.
_CLAIMS_PER_GARAGE = 1

# How long a battle is still worth asking about.
#
# A day. Long enough to cover a client closed mid-session and reopened the next
# evening, short enough that a battle nobody will ever answer for stops costing
# a request at every garage. The server's own ingest refuses what is too old
# anyway, so asking past that buys nothing.
_CLAIM_DEADLINE = 24 * 3600.0

# How many times one battle is asked about before it is let go of.
#
# A battle still running answers unsuccessfully, which is the ordinary case for
# the first ask: the player left early and it has not finished. Six garages is
# generous for that, and bounded for the battle whose results the server simply
# does not have.
_MAX_CLAIMS = 6

# Battles per request.
#
# Five rather than the twenty-five the endpoint accepts, and the measurement is
# why: a thirty-vehicle battle is 10.2 KB of JSON, so twenty-five of them would
# be a quarter of a megabyte in one call on a connection we know nothing about,
# and a failure would throw all of it away rather than a fifth of it. That is
# the same reasoning as the loadout upload's own batch, and the same numbers.
_BATCH = 5

# How long after the garage appears the queue is flushed. The garage has a
# sign-in, a carousel and the rest of this mod to draw first, and a battle
# that has already waited for the player to leave the results screen is not
# urgent.
_START_DELAY = 12.0

# How long after a battle's results land before trying to send it.
#
# Three seconds, and only to get off the stack of the screen that delivered
# them. It was twenty-five, to let the client finish drawing, and that number
# was both too big and aimed at the wrong thing: the garage finishes building
# its space BEFORE the results arrive, measured on a real session at 15:17:31
# against 15:17:53, so by the time the twenty-five seconds began the heavy part
# was already over. A stopwatch long enough for the slowest machine is also one
# that holds every other machine back, and it held a replay back with it, since
# the file is not collected until its battle has been named.
#
# What the wait was for is asked instead of timed now, in `flush`.
_SEND_DELAY = 3.0

# How often `flush` looks again while the garage is still building its space.
_WHILE_LOADING = 2.0

# How long it keeps looking before sending anyway.
#
# The results are what matter, which is this module's whole premise. Holding a
# battle report hostage to a graphics state would be a worse bug than the hitch
# the wait is there to avoid, so the wait gives up rather than wins.
_LOADING_DEADLINE = 30.0

# The least time between two flushes that actually sent something.
#
# Only a flush that reached the network counts, which is the fix for a real
# bug: the interval used to be stamped before the attempt, so the garage's
# flush at +12s consumed it and the battle's own at +25s was refused for the
# 13 seconds between them. The battle then waited for the next garage, which is
# after the next battle.
_MIN_INTERVAL = 60.0

# How long a flush may be in flight before another is allowed to start.
#
# `_busy` has callbacks for its only releases, and three paths never reach one.
# The worst is real rather than theoretical: the WGNI token requester is a
# process-wide singleton with ONE callback slot (TokenRequester.py), shared
# with the loadout upload and the client's own portal code, and whoever asks
# last overwrites the pending callback AND cancels the timeout that would have
# rescued it. Without a deadline here, one unlucky overlap stopped this client
# sending battles until the next reload, silently, while its queue filled up
# and started dropping the oldest.
_BUSY_DEADLINE = 90.0

# How long to stay quiet after the server says the quota is spent.
#
# Without this, every garage restarted the same burst against a server that
# had just said no, and the queue never drained. The server's own Retry-After
# is preferred when it sends one.
_QUOTA_BACKOFF = 900.0

# How many battles the queue keeps when the server cannot be reached.
#
# A queue that grew without a bound would be a file that grows without a
# bound. Two hundred battles is more than a week of heavy play, and past that
# the oldest go: a battle nobody could send for a week is one somebody else in
# it has almost certainly sent already. Dropping one is logged, because it is
# the only place this module loses data on purpose.
_MAX_QUEUED = 200

# The cluster, out of the results' own replayURL:
#     .../1790451702.EU.202.19011738950705142.59_asia_great_wall.wotsrvreplay
# Wargaming records every random battle server-side and hands the player the
# path to their own. Nothing public serves it, so what is kept is the cluster
# the battle ran on, which is a fact nobody else collects.
_CLUSTER = re.compile(r'\.([A-Z]{2,5})\.([0-9]{1,4})\.')

# The client version, out of `World of Tanks v.2.4.0.2 #966`. The exe reports
# only `2.4.0.0`, and the difference between 2.4.0.1 and 2.4.0.2 is exactly
# what decides whether a replay still plays.
_VERSION = re.compile(r'v\.\s*([0-9]+(?:\.[0-9]+)+)')

# The result's own key for each of ours. Renamed rather than passed through:
# `damageAssistedRadio` is the game's word for it and `radio` is ours, and the
# short names are the ones the site's column names were built from.
_FROM = {
    'team': 'team',
    'tank': 'typeCompDescr',
    'damage': 'damageDealt',
    'radio': 'damageAssistedRadio',
    'track': 'damageAssistedTrack',
    'stun': 'damageAssistedStun',
    'blocked': 'damageBlockedByArmor',
    'received': 'damageReceived',
    'shots': 'shots',
    'hits': 'directHits',
    'piercings': 'piercings',
    'spotted': 'spotted',
    'kills': 'kills',
    'lifeTime': 'lifeTime',
    'deathReason': 'deathReason',
    'capturePoints': 'capturePoints',
    'xp': 'xp',
    'credits': 'credits',
    'health': 'health',
    'maxHealth': 'maxHealth',
}

# The rest of what the game's own post-battle panel shows about one player,
# which the results carry and the roster has no column for.
#
# Sent only when non-zero, which is most of the saving: `sniper` is 0 on every
# vehicle that never shot past 300 metres, `repaired` on everyone without a
# repair kit used on an ally, and so on. Thirty vehicles carrying ten zeroes
# each would be a third of a battle's payload spent saying nothing.
_EXTRA = {
    # Damage dealt from more than 300 metres, which is the game's own wording.
    'sniper': 'sniperDamageDealt',
    'splash': 'explosionHits',
    'hitsReceived': 'directHitsReceived',
    'piercingsReceived': 'piercingsReceived',
    # Shots that landed and did nothing: the armour did its job.
    'bounced': 'noDamageDirectHitsReceived',
    # What would have landed had the armour not been there.
    'potential': 'potentialDamageReceived',
    'repaired': 'healthRepair',
    # Metres driven.
    'mileage': 'mileage',
    'defended': 'droppedCapturePoints',
    'teamDamage': 'tdamageDealt',
    # Enemy vehicles damaged, which the game shows beside the ones destroyed.
    'damaged': 'damaged',
}

# The battle-scoped vehicle id of whoever killed them, so the panel can name
# them the way the game does ("Destroyed by a shot (isakh)"). Not in `_EXTRA`
# because 0 is meaningful here: it is what the results say when nobody did.
_KILLER = 'killerID'

# Which of those are summed when a vehicle has more than one life, the rest
# describing the vehicle rather than its deeds. Measured: every battle in a
# 1424-replay sample had exactly one record per vehicle, Onslaught included,
# so this is for the respawn modes and for whatever the game adds next.
_SUMMED = ('damage', 'radio', 'track', 'stun', 'blocked', 'received', 'shots',
           'hits', 'piercings', 'spotted', 'kills', 'lifeTime',
           'capturePoints', 'xp', 'credits')

# What `deathReason` says when nobody killed them. Not 0: that is a real
# reason, and the commonest one.
SURVIVED = -1

# What the game's own Detailed Report shows about credits, experience and
# bonds, mapped from the results' own names.
#
# **This exists for the reporting client alone.** The results carry it under
# `personal.<vehicle>`, and the other twenty-nine players have no economy in
# the payload at any price: it is what this account earned, not a fact about
# the battle. So it is sent once per battle rather than per vehicle, and the
# site shows it only on the page of the player it belongs to.
_PERSONAL = {
    'creditsBase': 'originalCredits',
    'creditsBooster': 'boosterCredits',
    'creditsEvent': 'eventCredits',
    'creditsOrder': 'orderCredits',
    'creditsPenalty': 'creditsPenalty',
    'creditsCompensation': 'creditsContributionIn',
    'creditsSubtotal': 'subtotalCredits',
    'repairCost': 'autoRepairCost',
    'credits': 'credits',
    'xpBase': 'originalXP',
    'xpBooster': 'boosterXP',
    'xpEvent': 'eventXP',
    'xpPremiumVehicle': 'premiumVehicleXP',
    'xpPenalty': 'xpPenalty',
    'xp': 'xp',
    'freeXp': 'freeXP',
    'crewXp': 'tmenXP',
    'bonds': 'crystal',
    'bondsBase': 'originalCrystal',
    # The mark of excellence this battle left the gun at, as a whole percent.
    #
    # The state AFTER the battle, because that is the only one the results
    # carry: there is a `prevMarkOfMastery` for the mastery badge and nothing
    # of the kind for this, so what a single battle GAINED is the difference
    # with the last battle in the same tank, which is the server's job and not
    # the client's.
    #
    # Dropped when it is zero, like everything else in this table, and that is
    # exactly right here: Battle Royale reports zero because the mode does not
    # count towards marks at all, and writing that down would read as a gun
    # that fell back to nothing.
    'markPercent': 'damageRating',
}

# The two costs the results report as a list, `[credits, gold]`: only the
# credits half is spent by default, and a player who paid gold for shells did
# so deliberately and knows.
_PERSONAL_LISTS = {
    'ammoCost': 'autoLoadCost',
    'suppliesCost': 'autoEquipCost',
}


def cluster(replay_url):
    """'EU-202' out of the results' own replayURL, or None."""
    if not replay_url:
        return None
    found = _CLUSTER.search(replay_url)
    return '%s-%s' % (found.group(1), found.group(2)) if found else None


def client_version(full):
    """'2.4.0.2' out of 'World of Tanks v.2.4.0.2 #966', or None.

    The xml version rather than the exe's, which stops at `2.4.0.0`: a replay
    plays on the build it was recorded on and not on the one before it.
    """
    if not full:
        return None
    if isinstance(full, bytes):
        # A byte string holding a UTF-8 non-breaking space would raise on the
        # unicode replace below, and the only symptom would be every battle
        # carrying no client version at all.
        try:
            full = full.decode('utf-8')
        except UnicodeDecodeError:
            full = full.decode('latin-1')
    # The client writes these with non-breaking spaces.
    found = _VERSION.search(full.replace(u'\xa0', u' '))
    return found.group(1) if found else None


def _int(value, default=0):
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _vehicle(key, records):
    """One vehicle's whole battle, however many lives it took.

    The identity comes from the first record and the deeds are summed, because
    a vehicle that respawned did all of it. `health` and `deathReason` come
    from the LAST record: they describe how the battle left them, and the first
    life of a respawning vehicle always ends in a death.
    """
    first, last = records[0], records[-1]
    out = {'id': _int(key)}
    account = _int(first.get('accountDBID'))
    # A vehicle with no account is a bot, and has nobody to name.
    if account:
        out['account'] = account
    for ours, theirs in _FROM.items():
        out[ours] = _int(first.get(theirs))
    for ours in _SUMMED:
        out[ours] = sum(_int(record.get(_FROM[ours])) for record in records)
    out['health'] = _int(last.get('health'))
    out['deathReason'] = _int(last.get('deathReason'), SURVIVED)
    out['maxHealth'] = max(_int(record.get('maxHealth')) for record in records)
    # The medals the battle awarded them, by the game's own ids. Kept only when
    # there are any: they are empty on about 99 of every 100 vehicles, and an
    # empty list on every one of thirty would be a third of the payload saying
    # nothing. `inBattleAchievements` is always empty in the results, so it is
    # not read.
    medals = []
    for record in records:
        medals.extend(_int(one) for one in (record.get('achievements') or [])
                      if _int(one))
    if medals:
        out['medals'] = medals
    for ours, theirs in _EXTRA.items():
        total = sum(_int(record.get(theirs)) for record in records)
        if total:
            out[ours] = total
    # The marks painted on this gun, for EVERY vehicle in the battle and not
    # just ours: the results carry it in the public half, so a battle names the
    # marks of all thirty.
    #
    # From the last record and never summed. It is a state the vehicle arrived
    # with, so adding a respawn's together would give six marks on a gun that
    # has three. Kept only when there are any, like the medals above: thirty
    # vehicles carrying an explicit zero would be a third of the payload
    # saying nothing, and an absent mark reads as no mark.
    marks = _int(last.get('marksOnGun'))
    if marks:
        out['marks'] = marks
    killer = _int(last.get(_KILLER))
    if killer:
        out['killer'] = killer
    return out


def payload(results, arena, version):
    """What one battle is sent as, or None when these results do not describe one.

    `arena` is (map name, gameplay id) for the battle's arenaTypeID, and
    `version` the client's own: neither is in the results, both are in the
    client. Everything else is read from what the server sent.
    """
    if not isinstance(results, dict):
        return None
    arena_id = results.get('arenaUniqueID')
    common = results.get('common') or {}
    vehicles = results.get('vehicles') or {}
    started = _int(common.get('arenaCreateTime'))
    map_name, gameplay = arena if arena else (None, None)
    if not arena_id or not started or not map_name or not vehicles:
        return None
    rows = []
    for key, entry in vehicles.items():
        records = [record for record in (entry if isinstance(entry, list) else [entry])
                   if isinstance(record, dict)]
        if records:
            rows.append(_vehicle(key, records))
    if not rows:
        return None
    avatar = (results.get('personal') or {}).get('avatar') or {}
    out = {
        # Text, not a number: the game's id runs to 19 digits and no JSON
        # number survives that. The server's column is text for the same
        # reason, so the value that crosses is the value the game produced.
        'arenaUniqueId': str(arena_id),
        'startedAt': started,
        'mapName': map_name,
        'battleType': _int(common.get('bonusType')),
        'gameplayId': gameplay,
        'duration': _int(common.get('duration')),
        'winnerTeam': _int(common.get('winnerTeam')),
        'finishReason': _int(common.get('finishReason')),
        'clientVersion': version,
        'server': cluster(avatar.get('replayURL')),
        'vehicles': sorted(rows, key=lambda row: row['id']),
    }
    own = personal(results)
    if own:
        out['personal'] = own
    return out


def personal(results):
    """What this battle earned the reporting player, or None.

    Read from `personal.<vehicle>`, which is the one block the results carry
    about the client's own account rather than about the battle. Sent field by
    field and only when non-zero, so a battle that earned no bonds and fired no
    reserve carries neither.
    """
    if not isinstance(results, dict):
        return None
    own = None
    for key, value in (results.get('personal') or {}).items():
        if key != 'avatar' and isinstance(value, dict):
            own = value
            break
    if own is None:
        return None
    out = {}
    for ours, theirs in _PERSONAL.items():
        value = _int(own.get(theirs))
        if value:
            out[ours] = value
    for ours, theirs in _PERSONAL_LISTS.items():
        value = own.get(theirs)
        # A tuple, not a list: the client's own results schema declares
        # autoLoadCost and autoEquipCost as `tuple`, and DictPacker keeps that
        # type all the way to postResult. Testing for `list` alone sent every
        # battle up with no ammunition and no supplies cost, so the detailed
        # report showed neither and the credits never added up. The fixture in
        # tools/checks used lists, because it was lifted out of a .wotreplay,
        # whose second block is JSON -- which is how a check passed on data the
        # client never produces.
        cost = (_int(value[0]) if isinstance(value, (list, tuple)) and value
                else _int(value))
        if cost:
            out[ours] = cost
    if own.get('isPremium'):
        out['premium'] = True
    return out or None


def arena_names(arena_type_id):
    """(map name, gameplay id) for an arenaTypeID, from the client's own cache.

    `45_north_america` and `ctf`, the arena's names rather than the titles a
    player reads: a title is translated, and these have to mean the same thing
    in every client that sends one.
    """
    try:
        import ArenaType
        arena = ArenaType.g_cache.get(arena_type_id)
        if arena is None:
            return None
        return (getattr(arena, 'geometryName', None),
                getattr(arena, 'gameplayName', None))
    except Exception:
        _logger.exception('could not name arena %s', arena_type_id)
        return None


def full_version():
    """The client's own version, as '2.4.0.2', or None."""
    try:
        from helpers import getFullClientVersion
        return client_version(getFullClientVersion())
    except Exception:
        _logger.exception('could not read the client version')
        return None


def read_answer(body):
    """(stored, known, [(id, reason)]) from the endpoint, or None if it was not it.

    **None is the whole point of this function.** An answer the mod cannot
    parse and an answer that stored nothing have to be different things,
    because the caller deletes what the server names and the queue is the only
    copy. Reading a login page or a CDN error served with a 200 as "nothing was
    stored, carry on" is how a whole queue disappears in one flush.

    So an answer counts only if it names at least one of the three lists, and
    all of them are read by name rather than by position.
    """
    try:
        answer = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(answer, dict):
        return None
    stored, known, refused = [], [], []
    named = False
    for key, into in (('stored', stored), ('known', known)):
        value = answer.get(key)
        if isinstance(value, list):
            named = True
            into.extend(str(item) for item in value if item)
    rejected = answer.get('rejected')
    if isinstance(rejected, list):
        named = True
        for row in rejected:
            if isinstance(row, dict) and row.get('arenaUniqueId'):
                refused.append((str(row['arenaUniqueId']), row.get('reason') or '?'))
    return (stored, known, refused) if named else None


def load_queue():
    """The battles a previous session wrote down and could not send."""
    out = []
    try:
        with open(QUEUE, 'rb') as handle:
            lines = handle.read().decode('utf-8').splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            battle = json.loads(line)
        except ValueError:
            # One torn line, from a crash mid-append, is not worth the rest of
            # the queue.
            continue
        if isinstance(battle, dict) and battle.get('arenaUniqueId'):
            out.append(battle)
    return out


def append_queued(battle):
    """Add one battle to the file, without reading or rewriting it.

    This runs inside the battle results handler, on the thread that draws the
    game. One line is 10 KB and a fraction of a millisecond; serialising the
    whole queue here was 31 ms of `json.dumps` plus 12 ms of write, measured on
    200 battles, landing on the results screen's own animation.
    """
    try:
        folder = os.path.dirname(QUEUE)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(QUEUE, 'ab') as handle:
            handle.write(json.dumps(battle).encode('utf-8') + b'\n')
        return True
    except (IOError, OSError):
        _logger.exception('could not append to %s', QUEUE)
        return False


def save_queue(battles):
    """Rewrite the file. Only ever called once a flush has been answered."""
    try:
        folder = os.path.dirname(QUEUE)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(QUEUE, 'wb') as handle:
            handle.write(b''.join(json.dumps(battle).encode('utf-8') + b'\n'
                                  for battle in battles))
    except (IOError, OSError):
        _logger.exception('could not write %s', QUEUE)


def load_played():
    """The battles we were in and have not written down, as {id: startedAt}.

    `{}` on anything unreadable, like every other store here: a file a crash
    tore in half is a file we are better off rebuilding than refusing to start
    over.
    """
    try:
        with open(PLAYED, 'rb') as handle:
            parsed = json.loads(handle.read().decode('utf-8'))
    except (IOError, OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for key, value in parsed.items():
        if not isinstance(value, dict):
            continue
        try:
            out[str(key)] = {'at': float(value.get('at') or 0),
                             'asked': int(value.get('asked') or 0)}
        except (TypeError, ValueError):
            continue
    return out


def save_played(played):
    try:
        folder = os.path.dirname(PLAYED)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(PLAYED, 'wb') as handle:
            handle.write(json.dumps(played).encode('utf-8'))
    except (IOError, OSError):
        _logger.exception('could not write %s', PLAYED)


def claimable(played, settled, queued_ids, now):
    """The ids still worth asking the server about, oldest battle first.

    Already settled or already queued means written down, so there is nothing
    to ask. Past the deadline or past the attempt count means nobody is ever
    going to answer, and the entry is the caller's to drop.
    """
    out = []
    for arena_id, state in played.items():
        if arena_id in settled or arena_id in queued_ids:
            continue
        if now - state['at'] > _CLAIM_DEADLINE or state['asked'] >= _MAX_CLAIMS:
            continue
        out.append(arena_id)
    out.sort(key=lambda one: played[one]['at'])
    return out


def spent(played, settled, queued_ids, now):
    """The ids to forget: written down, too old, or asked about too often."""
    out = []
    for arena_id, state in played.items():
        if arena_id in settled or arena_id in queued_ids:
            out.append(arena_id)
        elif now - state['at'] > _CLAIM_DEADLINE or state['asked'] >= _MAX_CLAIMS:
            out.append(arena_id)
    return out


def load_settled():
    try:
        with open(SETTLED, 'rb') as handle:
            stored = json.loads(handle.read().decode('utf-8'))
    except (IOError, OSError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(stored, list):
        return []
    return [str(item) for item in stored if item][-_MAX_SETTLED:]


def save_settled(ids):
    try:
        folder = os.path.dirname(SETTLED)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(SETTLED, 'wb') as handle:
            handle.write(json.dumps(list(ids)[-_MAX_SETTLED:]).encode('utf-8'))
    except (IOError, OSError):
        _logger.exception('could not write %s', SETTLED)


def garage_ready():
    """Whether the garage has finished building its 3D space.

    The client's own account of it, read off `IHangarSpace.spaceInited`, which
    turns true in the callback the space's own `create` is handed. Asked rather
    than waited out, because the thing worth staying out of is a state, not a
    duration, and every duration long enough for the slowest machine is wrong
    on all the others.

    True when it cannot be read at all. A service that is not there is no reason
    to keep a battle for ever, and `flush`'s own `isPlayerAccount` has already
    established that we are somewhere a send belongs.
    """
    try:
        from helpers import dependency
        from skeletons.gui.shared.utils import IHangarSpace
        return bool(dependency.instance(IHangarSpace).spaceInited)
    except Exception:
        return True


def queued(battles, battle):
    """`battles` with `battle` added, deduplicated, oldest dropped past the cap."""
    out = [row for row in battles
           if row.get('arenaUniqueId') != battle['arenaUniqueId']]
    out.append(battle)
    return out[-_MAX_QUEUED:]


def settled(battles, done):
    """`battles` without the ids the server has answered for.

    Only the ids. A battle the answer did not mention stays in the queue, even
    when the call succeeded: a partial acceptance the client cannot see is
    exactly the case where dropping the batch loses data for good.
    """
    answered = set(done)
    return [row for row in battles if row.get('arenaUniqueId') not in answered]


class Reporter(object):

    def __init__(self, session, settings, link, motion=None, replays=None):
        self._session = session
        self._settings = settings
        self._link = link
        # Where everybody was, sampled by `motion` while the battle ran. Absent
        # when that module could not install, which costs the replay view and
        # nothing else.
        self._motion = motion
        # Told about each battle written down, so it can look for the client's
        # own `.wotreplay` at the next garage. Absent is the ordinary case for
        # anyone whose client keeps no replays, and costs nothing else.
        self._replays = replays
        self._queue = load_queue()
        self._settled = load_settled()
        # The battles we were in, until each is written down. See `PLAYED`.
        self._played = load_played()
        # The ones a request is already out for. Not persisted: a request
        # does not survive the client, and an id stuck in here across a
        # restart would be a battle never asked about again.
        self._asking = set()
        # When `flush` first found the garage still loading, so it can give
        # up after a while instead of looking for ever.
        self._loading_since = None
        self._busy_since = None
        self._last_flush = 0.0
        self._quiet_until = 0.0

    def install(self):
        """Read every mode's results, by patching the one place they all pass.

        See the module docstring: `onResultPosted` only carries readable
        results for the Gameface path, so a patch on `postResult` is what makes
        Ranked, Stronghold, CyberSport and everything else report at all.
        """
        try:
            from gui.battle_results.service import BattleResultsService
        except ImportError:
            _logger.exception('no battle results service; battles are not reported')
            return
        self._session.patch(BattleResultsService, 'postResult', self._wrap_post)
        try:
            from PlayerEvents import g_playerEvents
            # The garage, which is the one place the account is there to prove.
            self._session.subscribe(g_playerEvents.onAccountShowGUI, self._on_garage)
            # And the start of every battle, the same event `motion` watches.
            # This is what makes a battle recoverable: the client only posts
            # results it shows, so without a record of having BEEN there a
            # battle whose window never opened is one we cannot even know we
            # are missing.
            self._session.subscribe(g_playerEvents.onAvatarBecomePlayer, self._on_battle)
        except ImportError:
            _logger.exception('no player events; battles only flush after one lands')
        # The garage may already be up: the event fires when it appears, and a
        # mod installed after that would otherwise hold its queue until the
        # player next came back from a battle. `flush` refuses anywhere else.
        self._session.callback(_START_DELAY, self.flush)
        # And the same catch-up for the asking, for the same reason: a battle
        # the client never showed us is owed from the moment the mod loads, and
        # waiting for the next garage event means waiting for a whole battle.
        self._session.callback(_START_DELAY, self._claim)
        if self._replays is not None:
            # Everything the server has already answered for, handed over
            # before the first flush. This is the one gap a restart opens: a
            # battle settled in an earlier session is in no queue any more, so
            # no future answer will ever name it, and a replay still waiting on
            # its confirmation would wait for a word that is never coming.
            # Guarded on its own, like every other call into the archive.
            try:
                self._replays.confirm(self._settled)
            except Exception:
                _logger.exception('could not hand the settled battles to the '
                                  'replay archive')
        _logger.info('installed, %d battle(s) waiting', len(self._queue))

    def _wrap_post(self, original):
        """`postResult` with a look at the raw results on the way through.

        The client's own return value decides whether `requestResults`
        considers the battle posted, so it is handed back untouched and our
        own work is guarded whole: an exception here would make the client
        believe the results failed to arrive.
        """
        reporter = self

        def postResult(service, result, needToShowUI=True):
            try:
                reporter._record(result)
            except Exception:
                _logger.exception('could not write down a battle')
            return original(service, result, needToShowUI)

        return postResult

    def _sends(self):
        try:
            return self._settings.sends_battles()
        except Exception:
            _logger.exception('could not read the battle setting')
            return False

    def _record(self, results):
        """One battle's results, straight off the server. Write it down."""
        if not self._sends():
            return
        arena_id = str((results or {}).get('arenaUniqueID') or '')
        if arena_id and arena_id in self._settled:
            # Reopened from the notification centre: the client replays the
            # whole path for a cached battle, and this is the same battle.
            return
        common = (results or {}).get('common') or {}
        battle = payload(results,
                         arena_names(_int(common.get('arenaTypeID'), -1)),
                         full_version())
        if battle is None:
            _logger.info('battle %s carried nothing we could describe', arena_id or '?')
            return
        if any(row.get('arenaUniqueId') == battle['arenaUniqueId']
               for row in self._queue):
            return
        if self._motion is not None:
            # Taken and dropped, deliberately, until the server has somewhere to
            # put them.
            #
            # They were attached here and it broke every upload. Measured on one
            # real battle: the samples are 285 KB of raw JSON against 6 KB for
            # the whole result, and up to 3.6 MB on a long one with thirty
            # vehicles. Five of those is twenty megabytes against a 1.5 MB limit,
            # so nothing went up at all. The figure this was sized on, 64 KB, was
            # the GZIPPED size; the wire carries raw JSON, which is fifteen times
            # bigger.
            #
            # And the server would have thrown them away regardless: its body
            # schema has no `motion` field, so zod stripped them on arrival.
            #
            # Still taken rather than left in memory, so a battle's samples never
            # outlive it and get attached to the next one. To turn this back on:
            # a column or a bucket key to hold them, a field in the schema, and
            # zlib on this side, which is what makes 285 KB into 19 KB.
            try:
                self._motion.take(battle['arenaUniqueId'])
            except Exception:
                _logger.exception("could not clear the samples for this battle")
        if len(self._queue) >= _MAX_QUEUED:
            dropped = self._queue[0]
            _logger.warning('the queue is full at %d; dropping battle %s unsent',
                            _MAX_QUEUED, dropped.get('arenaUniqueId'))
        self._queue = queued(self._queue, battle)
        # Written down, so it is no longer owed. Dropped here rather than at
        # the garage so a battle reported the ordinary way never costs a
        # request: `_claim` only ever asks about what is still missing.
        if self._played.pop(str(battle['arenaUniqueId']), None) is not None:
            save_played(self._played)
        if append_queued(battle):
            _logger.info('battle %s written down, %d waiting',
                         battle['arenaUniqueId'], len(self._queue))
        if self._replays is not None:
            # Only told, not acted on: the file is looked for at the garage,
            # because this runs on the thread that draws the results screen and
            # the file is 1.3 MB. Guarded on its own so a fault in the archive
            # can never cost the battle itself, which is the part that matters.
            try:
                self._replays.offer(battle['arenaUniqueId'], battle.get('startedAt'))
            except Exception:
                _logger.exception('could not offer the replay of battle %s',
                                  battle['arenaUniqueId'])
        self._session.callback(_SEND_DELAY, self.flush)

    def _on_battle(self, *args):
        """A battle has started: remember we were in it.

        Written to disk at once rather than held in memory. The whole point is
        to survive the client being closed between the battle and the garage,
        which is one of the ways the results window never opens.
        """
        try:
            import BigWorld
            arena_id = str(getattr(BigWorld.player(), 'arenaUniqueID', '') or '')
            if not arena_id or arena_id in self._settled:
                return
            if arena_id in self._played:
                return
            self._played[arena_id] = {'at': time.time(), 'asked': 0}
            save_played(self._played)
        except Exception:
            _logger.exception('could not write down being in a battle')

    def _on_garage(self, *args):
        try:
            self._session.callback(_START_DELAY, self.flush)
            self._session.callback(_START_DELAY, self._claim)
        except Exception:
            # onAccountShowGUI is a SafeEvent (events_container._createEvent),
            # so the client would swallow this anyway and keep calling the
            # listeners after us. Guarded all the same, to say in the log that
            # the flush was never scheduled rather than leave it a silence.
            _logger.exception('could not schedule a flush at the garage')

    def _claim(self):
        """Ask the client for the results of a battle it never showed us.

        `requestResults` fetches them and hands them to `postResult`, which is
        the method this module already patches, so there is no second extractor
        to keep in step with the first. Both show flags are off: nothing opens,
        and the player sees none of this.

        One battle per garage, oldest first. Each ask is a real request to the
        server for a real battle, and somebody installing the mod with a
        fortnight behind them should not open the garage into a hundred of them.
        """
        # In an arena rather than the garage, `requestResults` reaches
        # BigWorld.player().battleResultsCache, which a PlayerAvatar has not
        # got: the client's own processor raises AttributeError inside an adisp
        # call. `flush` has always been gated on this; this was not, and it is
        # scheduled twelve seconds after the garage event, which is inside the
        # loading screen for anybody who presses Battle straight away. Six such
        # garages and a battle is given up on, having never been asked for.
        try:
            from helpers import isPlayerAccount
            if not isPlayerAccount():
                return
        except ImportError:
            return
        now = time.time()
        queued_ids = set(str(row.get('arenaUniqueId')) for row in self._queue)
        for arena_id in spent(self._played, self._settled, queued_ids, now):
            state = self._played.pop(arena_id)
            if state['asked'] >= _MAX_CLAIMS:
                _logger.info('battle %s never answered after %d asks; letting it go',
                             arena_id, state['asked'])
        wanted = [one for one in claimable(self._played, self._settled,
                                          queued_ids, now)
                  if one not in self._asking]
        if not wanted:
            save_played(self._played)
            return
        try:
            from helpers import dependency
            from skeletons.gui.battle_results import IBattleResultsService
            from gui.battle_results.context import RequestResultsContext
            service = dependency.instance(IBattleResultsService)
        except Exception:
            _logger.exception('cannot ask for battle results; %d battle(s) unclaimed',
                              len(wanted))
            save_played(self._played)
            return
        for arena_id in wanted[:_CLAIMS_PER_GARAGE]:
            # Counted as spent only once the dispatch has returned without
            # raising: an ask that never left the client is not a try, and six
            # of those used to be enough to abandon a battle for good.
            asked = self._played[arena_id]['asked'] + 1
            # In flight, so the next pass leaves it alone. `install` and the
            # garage event each schedule a pass, forty seconds apart, and
            # without this the second asked again for a battle the first was
            # still fetching: seen in the log as a third ask 378ms after the
            # battle it was asking about had been written down.
            self._asking.add(arena_id)
            try:
                # `showImmediately` AND `showIfPosted` off: the results go
                # through `postResult` and nowhere near a window.
                # `requestResults(ctx)(callback)`, in two calls, which is not a
                # style choice. `adisp_async` returns a closure that takes the
                # callback and only THEN runs the function (adisp.py:82), so
                # `requestResults(ctx, callback)` runs nothing at all: the
                # callback lands in `*args`, the closure is dropped, and the
                # request is never made. The log said "asking for it" and the
                # client was never asked.
                service.requestResults(
                    RequestResultsContext(int(arena_id), showImmediately=False,
                                          showIfPosted=False))(
                    lambda ok, which=arena_id: self._answered(which, ok))
                self._played[arena_id]['asked'] = asked
                _logger.info('battle %s was never shown to us; asking for it '
                             '(try %d of %d)', arena_id, asked, _MAX_CLAIMS)
            except Exception:
                self._asking.discard(arena_id)
                _logger.exception('could not ask for the results of battle %s',
                                  arena_id)
        save_played(self._played)

    def _answered(self, arena_id, ok):
        """The client has finished with a request, whatever it found.

        Only clears the in-flight mark. A successful answer has already gone
        through `postResult` and so through `_record`, which is what takes
        the battle off the owed list; an unsuccessful one leaves it owed, to
        be asked again at the next garage until its count runs out.
        """
        self._asking.discard(arena_id)
        _logger.info('the client answered for battle %s: %s', arena_id,
                     'results in hand' if ok else 'no results yet')

    def _free(self, now):
        """Whether no flush is in flight, or the one that is has run too long."""
        if self._busy_since is None:
            return True
        if now - self._busy_since < _BUSY_DEADLINE:
            return False
        _logger.warning('a flush has been in flight for %.0fs with no answer; '
                        'starting another', now - self._busy_since)
        return True

    def flush(self):
        """Send what is waiting, a batch at a time."""
        if not self._queue or not self._sends():
            return
        now = time.time()
        if not self._free(now):
            return
        if now < self._quiet_until:
            return
        if now - self._last_flush < _MIN_INTERVAL:
            return
        try:
            from helpers import isPlayerAccount
            if not isPlayerAccount():
                return
        except ImportError:
            return
        if not garage_ready():
            # Still building the 3D space, which is the one moment in the client
            # worth staying out of. Looked at again rather than waited out, and
            # given up on rather than waited for: see `_LOADING_DEADLINE`.
            if self._loading_since is None:
                self._loading_since = now
            if now - self._loading_since < _LOADING_DEADLINE:
                self._session.callback(_WHILE_LOADING, self.flush)
                return
            _logger.info('the garage has not finished loading in %.0fs; '
                         'sending anyway', now - self._loading_since)
        self._loading_since = None
        self._busy_since = now
        self._post()

    def _done(self):
        self._busy_since = None

    def _post(self):
        # Re-read every batch: unticking the box has to stop the flush that is
        # already running, not only the next one. A full queue is forty calls.
        if not self._sends():
            _logger.info('battle sharing turned off; %d battle(s) stay on disk',
                         len(self._queue))
            self._done()
            return
        batch = self._queue[:_BATCH]
        if not batch:
            self._done()
            return
        body = json.dumps({'battles': batch})

        def answered(response):
            code = getattr(response, 'responseCode', None)
            if code == 429:
                # Back off rather than come straight back at the next garage,
                # which is what turned a spent quota into a permanent burst.
                wait = _seconds(getattr(response, 'responseHeaders', None)) or _QUOTA_BACKOFF
                self._quiet_until = time.time() + wait
                self._done()
                _logger.warning('the server is rate limiting us; quiet for %.0fs, '
                                '%d battle(s) still waiting', wait, len(self._queue))
                return
            if code != 200:
                # Kept, not dropped: a battle a network refused is one the next
                # garage sends.
                self._done()
                _logger.warning('battle upload stopped on HTTP %s, %d still waiting',
                                code, len(self._queue))
                return
            answer = read_answer(getattr(response, 'body', None))
            if answer is None:
                # A 200 carrying something that is not our answer. Nothing is
                # named, so nothing is forgotten.
                self._done()
                _logger.warning('the server answered 200 with something that is not '
                                'an upload result; %d battle(s) kept',
                                len(self._queue))
                return
            stored, known, refused = answer
            # Exactly what was named, and nothing else.
            names = list(stored) + list(known) + [id_ for id_, _ in refused]
            if not names:
                self._done()
                _logger.warning('the server named no battle at all; %d kept',
                                len(self._queue))
                return
            self._queue = settled(self._queue, names)
            self._settled = (self._settled + names)[-_MAX_SETTLED:]
            save_queue(self._queue)
            save_settled(self._settled)
            if self._replays is not None:
                # The server has spoken, so the replays of these battles can
                # finally be looked for: a file offered before its own battle
                # is refused with `unknown_battle`, and the archive used to
                # read that as a verdict and delete the player's only copy.
                #
                # `stored` and `known` only. A refused battle will never have a
                # row, so its replay is told to go rather than left to be
                # offered and refused until its own bound runs out.
                try:
                    self._replays.confirm(list(stored) + list(known))
                    if refused:
                        self._replays.disown([id_ for id_, _ in refused])
                except Exception:
                    _logger.exception('could not tell the replay archive which '
                                      'battles the server named')
            if refused:
                _logger.warning('%d battle(s) refused for good: %s', len(refused),
                                ', '.join('%s %s' % pair for pair in refused))
            _logger.info('%d stored, %d already known, %d waiting',
                         len(stored), len(known), len(self._queue))
            if self._queue:
                self._post()
            else:
                self._done()

        self._last_flush = time.time()
        self._request('%s/api/game/battles' % config.API_BASE.rstrip('/'), answered,
                      method='POST', post_data=body)

    def _request(self, url, answered, method='GET', post_data=''):
        """Signed with whatever proves this account, and nothing if neither does.

        The same two proofs as the loadout upload: the link's secret when the
        player has one, otherwise the client's own WGNI web token, which
        unicum.gg has Wargaming confirm. A battle is a statement about thirty
        accounts and the server checks that this one is among them, which it
        can only do if it knows which account is speaking.
        """
        secret = getattr(self._link, 'secret', None)
        if secret:
            self._fetch(url, answered, {'Authorization': 'Bearer %s' % secret},
                        method, post_data)
            return

        def with_token(response):
            if not (response and response.isValid()):
                _logger.info('no web token; battles wait for the next garage')
                self._done()
                return
            self._fetch(url, answered, {
                'X-Wargaming-Token': str(response.getToken()),
                'X-Wargaming-Region': config.REGION,
            }, method, post_data)

        try:
            from constants import TOKEN_TYPE
            from gui.shared.utils.requesters import getTokenRequester
            requester = getTokenRequester(TOKEN_TYPE.WGNI)
            if requester.isInProcess():
                # Somebody else holds the singleton's one callback slot. The
                # deadline in `_free` is what makes this recoverable rather
                # than final.
                self._done()
                return
            requester.request(timeout=10.0)(with_token)
        except Exception:
            _logger.exception('could not ask for a web token')
            self._done()

    def _fetch(self, url, answered, headers, method, post_data):
        headers = dict(headers)
        headers['Content-Type'] = 'application/json'
        self._session.fetch(url, answered, headers=headers,
                            timeout=config.API_TIMEOUT, method=method,
                            post_data=post_data)


def _seconds(headers):
    """The server's own Retry-After in seconds, or None."""
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if str(key).lower() == 'retry-after':
            seconds = _int(value, 0)
            return seconds if 0 < seconds <= 86400 else None
    return None


def install(session, settings, link, motion=None, replays=None):
    reporter = Reporter(session, settings, link, motion, replays)
    reporter.install()
    return reporter
