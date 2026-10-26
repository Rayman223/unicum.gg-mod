"""Sending the battle's own `.wotreplay` file, when the client kept one.

Why a file at all
-----------------
`motion.py` already samples every vehicle five times a second, and that is what
the site draws: 64 KB against this file's 1.3 MB. So the file is not how a
battle gets replayed. It is insurance for a 3D view nobody has built yet, which
would want shell trajectories, the geometry of each hit and the state of the
destructible terrain. None of that can be collected after the fact, and a
battle played today is the only chance to have it.

Why it has to be copied aside
-----------------------------
**The file is volatile.** `BattleReplay.enableAutoRecordingBattles` is 1 by
default, which calls `setResultingFileName(FIXED_REPLAY_FILENAME, True)`: every
battle is written to `replays/replay_last_battle.wotreplay` and **overwrites
the one before it**. A player who queues up again while an upload is in flight
destroys the thing being uploaded. So the file is copied into our own folder at
the garage, and the upload reads the copy.

Players who set recording to "all" get a descriptive name per battle instead,
and nothing is overwritten. Both are handled by looking the file up by the
battle id inside it rather than by trusting any name.

Why base64
----------
The body goes out through `BigWorld.fetchURL`, whose `postData` the game itself
never uses with anything but text, and whose binary safety is documented
nowhere. A replay is full of zero bytes. If that path is not binary-safe the
failure is a file truncated at the first one, which still has a valid header
and still names the right battle, and would be archived silently broken. 33% on
the wire, once per battle, buys certainty instead. The SHA-256 that rides along
catches the other half of the problem: a body cut short in transit.
"""
import base64
import hashlib
import json
import logging
import os
import struct
import time

from unicum import config

_logger = logging.getLogger('unicum.replays')


#: Where the client writes replays, from `BattleReplay.__replayDir`.
REPLAY_DIR = os.path.join('replays')

#: The recording in progress, which is never a finished battle.
_IN_PROGRESS = 'temp.wotreplay'

#: Our own copies, so the client may overwrite its originals freely.
STASH = os.path.join('mods', 'configs', 'unicum', 'replays')

#: What we still owe the server, as [{arenaUniqueId, startedAt, file}].
QUEUE = os.path.join('mods', 'configs', 'unicum', 'replays.json')

#: Battles reported but whose file has not been looked for yet, {id: startedAt}.
#:
#: On disk rather than in memory alone, because the two moments are minutes
#: apart: the results arrive when the battle ends, the file is collected at the
#: garage. A client closed in between, or a dev reload, would otherwise drop the
#: only record that a file was ever wanted, and the battle's replay is gone for
#: good once the next one overwrites it.
WANTED = os.path.join('mods', 'configs', 'unicum', 'replays-wanted.json')

#: How many copies may wait on disk at once.
#:
#: Twenty is 26 MB at the measured average of 1.3 MB, which is a rounding error
#: on a 70 GB game install and still a bound rather than a hope. Past it the
#: oldest is dropped: a replay is a nice-to-have, and filling somebody's disk
#: to keep one would be a poor trade.
_MAX_QUEUED = 20

#: Only the newest few files are opened when looking for a battle.
#:
#: A player on "all" can have twelve thousand of them, and reading every header
#: to find one battle would be minutes of disk. The file we want was written
#: moments ago, so the newest handful always contains it.
_CANDIDATES = 6

#: Enough of the front of a file to hold both JSON blocks.
#:
#: The metadata and the results sit in the clear before the encrypted stream.
#: 256 KB is far more than either needs and avoids pulling a megabyte off disk
#: to read a number.
_HEADER_BYTES = 262144

#: Magic at the top of every replay.
_MAGIC = b'\x12\x32\x34\x11'

#: Seconds after reaching the garage before anything is collected or sent.
#:
#: Later than the results flush on purpose: the results are what matter, and
#: the garage is already the busiest moment in the client.
_START_DELAY = 40.0

#: The least time between two uploads. One file is 1.7 MB encoded.
_MIN_INTERVAL = 20.0

# How many times a copy may be refused before the rest of the queue is let
# past it. ONE, because trying the same copy again in the same minute asks
# the same server the same question: the point is not to give up on it, it is
# to stop one copy holding everything else hostage. It comes back round after
# the others have had their turn, which is retry enough.
_MAX_FAILURES = 1

#: How long a flush may be unanswered before another is allowed to start.
_BUSY_DEADLINE = 120.0

#: How long to stay quiet when the server says it keeps no replays, or that its
#: archive is full. Both are "stop asking", not "try again shortly".
_CLOSED_BACKOFF = 86400.0

# How long a copy waits to be named by the server before it is let go of.
#
# A copy is made as soon as the file exists but only OFFERED once the battle has
# been named, so a battle the server never names leaves a copy nothing will ever
# send. A try counter cannot bound that: nothing counts an upload that never
# happens. The deadline is read off the copy's own `startedAt`, which it already
# carries, so it survives a restart for free.
#
# Twelve hours. The reporter's record of answered ids is capped at five hundred,
# so a confirmation really can be missed outright, and this is the backstop for
# exactly that.
_CONFIRM_DEADLINE = 12 * 3600.0

# How many times one copy is offered for a battle the server does not have.
#
# Six, against a collection that runs once per garage: a file survives a
# whole evening of the server being behind, and is still let go of if its
# battle genuinely never lands (results refused for good, a row removed).
_MAX_TRIES = 6

# How long after the battle is confirmed before its file is looked for.
#
# Not `_START_DELAY`. That one is forty seconds because the garage is the
# busiest moment in the client and the results are what matter; a
# confirmation arrives twenty-five seconds after the results, which is itself
# well after the garage has finished drawing, so the reason for waiting has
# already passed. Borrowing it put seventy seconds between the end of a
# battle and its replay being readable, for no reason anybody could name.
#
# And the work is small: measured on a real 0.97 MB replay under the client's
# own Python, 1 ms to read, 5 ms to base64, 2 ms to hash. Eight milliseconds,
# half a frame. Three seconds is only to get off the answer handler's own
# stack, not to spare the client anything.
_AFTER_CONFIRMED = 3.0

#: And when it rate limits us without saying for how long.
_QUOTA_BACKOFF = 900.0


def _u32(data, at):
    return struct.unpack_from('<I', data, at)[0]


def battle_in(path):
    """The arena id this replay belongs to, or None if it says nothing.

    Reads the JSON blocks only. They are not encrypted, so this never touches
    Blowfish and never inflates anything; it is a few KB off the front of the
    file. The id lives in the SECOND block, the one written when the battle
    ends, so a replay of a battle still in progress answers None and is
    correctly left alone.
    """
    try:
        with open(path, 'rb') as handle:
            head = handle.read(_HEADER_BYTES)
    except (IOError, OSError):
        return None
    if len(head) < 12 or head[:4] != _MAGIC:
        return None
    try:
        count = _u32(head, 4)
        if count > 16:
            return None
        at = 8
        blocks = []
        for _ in range(count):
            if at + 4 > len(head):
                return None
            size = _u32(head, at)
            at += 4
            if at + size > len(head):
                # The block runs past what we read. Only possible for the
                # stream, which we do not want anyway.
                break
            blocks.append(head[at:at + size])
            at += size
    except Exception:
        return None
    for raw in blocks[1:]:
        try:
            parsed = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(parsed, list) and parsed:
            parsed = parsed[0]
        if isinstance(parsed, dict) and parsed.get('arenaUniqueID'):
            return str(parsed['arenaUniqueID'])
    return None


def candidates():
    """The newest finished replays on disk, newest first."""
    try:
        names = [n for n in os.listdir(REPLAY_DIR)
                 if n.lower().endswith('.wotreplay') and n != _IN_PROGRESS]
    except (IOError, OSError):
        return []
    paths = []
    for name in names:
        full = os.path.join(REPLAY_DIR, name)
        try:
            paths.append((os.path.getmtime(full), full))
        except (IOError, OSError):
            continue
    paths.sort(reverse=True)
    return [full for _, full in paths[:_CANDIDATES]]


def find(arena_id):
    """The file for this battle, found by what is inside it, not by its name.

    By content on purpose: the default setting writes every battle to the same
    name, the "all" setting builds one out of the map and the tank, and neither
    is a promise. The id in the file is.
    """
    for path in candidates():
        if battle_in(path) == str(arena_id):
            return path
    return None


def stash(arena_id, source):
    """Copy the file somewhere the client will not overwrite it."""
    try:
        if not os.path.isdir(STASH):
            os.makedirs(STASH)
        target = os.path.join(STASH, '%s.wotreplay' % arena_id)
        with open(source, 'rb') as reading:
            data = reading.read()
        if not data:
            return None
        with open(target, 'wb') as writing:
            writing.write(data)
        # Read back what we actually copied, because `find` read the id out of
        # this path and we re-read the path to copy it: by default the client
        # writes every battle to the same name and overwrites it, so a player
        # who queued up in between leaves us a copy of a DIFFERENT battle under
        # this battle's name. The server then answers `not_this_battle`, which
        # is how this was found: battle 90570953574329799 was refused at
        # 01:19:07 and the same battle stored at 01:38:42, once a later pass had
        # copied the right file.
        #
        # A few kilobytes off the front of our own copy, no decryption and no
        # inflation, against a megabyte uploaded for nothing.
        if battle_in(target) != str(arena_id):
            _logger.warning('the file for battle %s changed under us; '
                            'not keeping that copy', arena_id)
            try:
                os.remove(target)
            except (IOError, OSError):
                pass
            return None
        return target
    except (IOError, OSError):
        _logger.exception('could not copy the replay for battle %s', arena_id)
        return None


def forget(entry):
    """Drop our copy of one battle's file."""
    path = (entry or {}).get('file')
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except (IOError, OSError):
        _logger.exception('could not remove %s', path)


def load_queue():
    try:
        with open(QUEUE, 'rb') as handle:
            parsed = json.loads(handle.read().decode('utf-8'))
    except (IOError, OSError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [row for row in parsed
            if isinstance(row, dict) and row.get('arenaUniqueId')]


def save_queue(entries):
    try:
        folder = os.path.dirname(QUEUE)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(QUEUE, 'wb') as handle:
            handle.write(json.dumps(entries).encode('utf-8'))
    except (IOError, OSError):
        _logger.exception('could not write %s', QUEUE)


def load_wanted():
    try:
        with open(WANTED, 'rb') as handle:
            parsed = json.loads(handle.read().decode('utf-8'))
    except (IOError, OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for key, value in parsed.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def save_wanted(wanted):
    try:
        folder = os.path.dirname(WANTED)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(WANTED, 'wb') as handle:
            handle.write(json.dumps(wanted).encode('utf-8'))
    except (IOError, OSError):
        _logger.exception('could not write %s', WANTED)


def read_answer(body):
    """('stored'|'refused', detail) from the endpoint, or None if it was not it.

    None kept apart from every other answer for the reason `battles.read_answer`
    keeps it apart: the caller deletes a file on the strength of this, and
    reading a login page served with a 200 as "stored, you may delete it" is
    how an archive quietly loses what it was built to keep.
    """
    try:
        answer = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(answer, dict):
        return None
    if answer.get('stored'):
        return ('stored', str(answer['stored']))
    if answer.get('refused'):
        return ('refused', str(answer['refused']))
    return None


class Uploader(object):
    """Collects the file of each battle at the garage, and sends it."""

    #: Answers that mean this battle will never be wanted. The copy goes.
    FINAL = ('already_stored', 'not_a_player')
    #: Answers that mean no battle will be wanted for a while. Everything goes.
    CLOSED = ('not_collected', 'archive_full')
    #: Answers that only mean "not yet". The copy is kept and offered again.
    #
    # `unknown_battle` used to sit in FINAL, which is the bug this tuple
    # exists to fix. It is not a verdict on the battle, it is a statement
    # about WHEN we asked: the results are queued and sent on a timer that
    # starts when they land, the replay on one that starts at the garage, so
    # the replay normally asks first. Measured on one real session, the
    # replay arrived 17s, 2s and 2s before its own battle, and the one time
    # it won it won by 137ms. Deleting the player's only copy on the strength
    # of a tenth of a second lost three battles in one evening.
    PENDING = ('unknown_battle',)
    #: Answers that condemn the COPY but not the battle. Look again.
    #
    # `not_this_battle` means the bytes we sent hold another battle, which
    # is true and is our own doing: the client rewrites
    # `replay_last_battle.wotreplay` between the moment the id is read out
    # of it and the moment it is copied. The copy is wrong, the battle is
    # still owed, and the right file may still be on disk. It sat in FINAL,
    # so a copy taken a second too late lost the replay for good: battle
    # 90570953574329799 was refused at 01:19:07 and only stored at 01:38:42
    # because the battle happened to be reported a second time.
    RETAKE = ('not_this_battle',)

    def __init__(self, session, settings, link):
        self._session = session
        self._settings = settings
        self._link = link
        self._queue = load_queue()
        # Battles whose results have been reported but whose file has not been
        # looked for yet: {arenaUniqueId: startedAt}. Read back from disk, so a
        # client closed between the battle and the garage still collects it.
        self._wanted = load_wanted()
        # The battles the server has said it holds, told to us by the battle
        # reporter. Deliberately NOT persisted: the reporter reads its own
        # record of answered ids off disk at install and hands it straight
        # back, so a second copy here would be the same truth kept twice and
        # the two would disagree the first time one of them was not written.
        self._known = set()
        self._busy_since = None
        self._quiet_until = 0.0
        self._last_flush = 0.0

    def install(self):
        try:
            from PlayerEvents import g_playerEvents
            self._session.subscribe(g_playerEvents.onAccountShowGUI, self._on_garage)
        except ImportError:
            _logger.exception('no player events; replays are not collected')
            return
        # The garage may already be up: `onAccountShowGUI` fires when it
        # appears, so a module installed after that (every dev reload) would
        # hold what it is owed until the player next came back from a battle.
        self._session.callback(_START_DELAY, self._collect)
        _logger.info('installed, %d replay(s) waiting, %d wanted',
                     len(self._queue), len(self._wanted))

    def offer(self, arena_id, started_at):
        """Told by the battle reporter that this battle has been written down.

        Only remembered here. The file is not looked for until the garage: the
        client is still finishing the results screen, and reading a megabyte
        off disk on the thread that draws the game is not something to do while
        an animation is running.
        """
        if not arena_id or not started_at:
            return
        self._wanted[str(arena_id)] = int(started_at)
        save_wanted(self._wanted)

    def confirm(self, arena_ids):
        """Told which battles the server says it holds. Collection waits for this.

        A replay offered before its own battle earns `unknown_battle`, which is
        the server answering honestly about a row that does not exist yet. The
        file is good; only the question was early. So the file is not looked
        for at all until the reporter has heard the battle named, which turns a
        race decided by a tenth of a second into an order.

        Called with the whole record at install as well as with each answer,
        because the gap this closes is exactly the one a restart opens: a
        battle settled in a previous session is in no queue any more and will
        never be named again.
        """
        fresh = set(str(one) for one in arena_ids if one)
        if not fresh:
            return
        self._known |= fresh
        # `flush`, not `_collect`. The copy is made at the garage whatever the
        # server knows, because the client overwrites `replay_last_battle` with
        # the next battle and a copy that waited would be a copy of the wrong
        # battle or of nothing. Only the offer waits for this, so what a
        # confirmation unblocks is the sending.
        #
        # A battle confirmed before its copy exists needs no nudge: `_collect`
        # ends with its own `flush`, and by then this id is in `_known`.
        if any(str(row.get('arenaUniqueId')) in fresh for row in self._queue):
            try:
                self._session.callback(_AFTER_CONFIRMED, self.flush)
            except Exception:
                _logger.exception('could not schedule the replay upload')

    def disown(self, arena_ids):
        """Told which battles the server refused for good. Their files go.

        Kept apart from `confirm` on purpose. Both mean the server has spoken,
        but a refused battle will never have a row, so its replay would be
        offered six times and dropped on the sixth. Saying so now costs one
        line and saves six megabytes of upload.
        """
        gone = set(str(one) for one in arena_ids if one)
        if not gone:
            return
        for arena_id in [key for key in self._wanted if key in gone]:
            del self._wanted[arena_id]
            _logger.info('battle %s was refused for good; its replay is no '
                         'longer wanted', arena_id)
        for entry in [row for row in self._queue
                      if str(row.get('arenaUniqueId')) in gone]:
            forget(entry)
            self._queue.remove(entry)
        save_wanted(self._wanted)
        save_queue(self._queue)

    def _sends(self):
        try:
            return self._settings.sends_replays()
        except Exception:
            _logger.exception('could not read the replay setting')
            return False

    def _on_garage(self, *args):
        try:
            self._session.callback(_START_DELAY, self._collect)
        except Exception:
            _logger.exception('could not schedule the replay collection')

    def _collect(self):
        """Find and copy the files of the battles we were told about."""
        if not self._sends():
            self._wanted = {}
            save_wanted(self._wanted)
            return
        if time.time() < self._quiet_until:
            # KEPT, not forgotten. This branch is reached after a 429, which
            # means "ask again later" and nothing else: the files are still on
            # disk and the battles still want their replay. Emptying the list
            # here threw away every pending replay on the first rate limit,
            # and the quiet period is 15 minutes, so what was discarded was
            # whatever the player had just played.
            #
            # The case that DOES mean forget is a server that keeps no
            # replays at all, and `_drop_all` says so itself: it clears the
            # list and sets a day of quiet in the same breath.
            return
        for arena_id, started_at in list(self._wanted.items()):
            del self._wanted[arena_id]
            if any(row.get('arenaUniqueId') == arena_id for row in self._queue):
                continue
            source = find(arena_id)
            if source is None:
                # No file. By far the commonest outcome and not a failure: the
                # player may have recording off entirely.
                _logger.info('no replay on disk for battle %s', arena_id)
                continue
            target = stash(arena_id, source)
            if target is None:
                continue
            while len(self._queue) >= _MAX_QUEUED:
                dropped = self._queue.pop(0)
                _logger.warning('replay queue full at %d; dropping %s',
                                _MAX_QUEUED, dropped.get('arenaUniqueId'))
                forget(dropped)
            self._queue.append({'arenaUniqueId': arena_id,
                                'startedAt': started_at,
                                'file': target})
            _logger.info('replay kept for battle %s, %d waiting',
                         arena_id, len(self._queue))
        save_wanted(self._wanted)
        save_queue(self._queue)
        self.flush()

    def _free(self, now):
        if self._busy_since is None:
            return True
        if now - self._busy_since < _BUSY_DEADLINE:
            return False
        _logger.warning('a replay upload has been in flight for %.0fs; starting another',
                        now - self._busy_since)
        return True

    def flush(self):
        if not self._queue or not self._sends():
            return
        now = time.time()
        if not self._free(now) or now < self._quiet_until:
            return
        if now - self._last_flush < _MIN_INTERVAL:
            return
        try:
            from helpers import isPlayerAccount
            if not isPlayerAccount():
                return
        except ImportError:
            return
        self._busy_since = now
        self._post()

    def _done(self):
        self._busy_since = None

    def _next(self):
        """The first copy whose battle the server has said it holds.

        Not `self._queue[0]`. A copy is kept from the moment its file exists,
        but offered only once the battle has been named, so the queue holds
        copies in both states at once and its head is quite often the one that
        cannot go yet. Taking the head regardless would park every confirmed
        copy behind one unconfirmed battle until that battle's own deadline.

        Copies nobody ever names are let go of here, on their `startedAt`,
        because `_retry`'s counter only ever sees uploads that happened.
        """
        now = time.time()
        chosen = None
        stale = []
        for entry in self._queue:
            if str(entry.get('arenaUniqueId')) in self._known:
                if chosen is None:
                    chosen = entry
            elif now - int(entry.get('startedAt') or 0) > _CONFIRM_DEADLINE:
                stale.append(entry)
        for entry in stale:
            forget(entry)
            self._queue.remove(entry)
            _logger.info('battle %s was never named by the server in %.0fh; '
                         'dropping its copy', entry.get('arenaUniqueId'),
                         _CONFIRM_DEADLINE / 3600.0)
        if stale:
            save_queue(self._queue)
        return chosen

    def _wrong_copy(self, entry):
        """This copy holds another battle. Drop it and look for the right file.

        The copy is genuinely wrong, so it goes; but the BATTLE is still owed,
        and the file may well still be on disk under another name. Letting the
        want go here is what made this permanent: the client rewrites
        `replay_last_battle.wotreplay` between the moment `find` reads the id
        out of it and the moment `stash` copies it, so the copy can be of the
        next battle. `stash` checks for that now, and this is the net for a copy
        that was already taken before it did.

        The next pass either finds the right file or finds nothing, says so and
        forgets it, so this cannot loop.
        """
        arena_id = str(entry.get('arenaUniqueId'))
        forget(entry)
        self._queue = [row for row in self._queue
                       if str(row.get('arenaUniqueId')) != arena_id]
        started = int(entry.get('startedAt') or 0)
        if started:
            self._wanted[arena_id] = started
            save_wanted(self._wanted)
        save_queue(self._queue)
        _logger.info('the copy for battle %s was of another battle; dropped, '
                     'and the file is wanted again', arena_id)
        self._done()

    def _set_aside(self, entry, code):
        """Let the rest of the queue past a copy the server keeps refusing.

        Keeping a copy after a failure is right: a network that refused one
        upload is one the next garage retries. What was wrong is that
        `_next` picks the FIRST confirmed copy every time, so a copy that
        fails every time is picked every time, and everything behind it waits
        on a battle that is never going up. One replay the server would not
        take stopped twelve from being sent, and the only sign of it was the
        player noticing their profile had no recent battles.

        The copy goes to the back rather than being dropped: the refusal may
        be the server's to fix, and the file costs a megabyte on disk.
        """
        failures = int(entry.get('failures') or 0) + 1
        entry['failures'] = failures
        if failures < _MAX_FAILURES:
            save_queue(self._queue)
            return
        entry['failures'] = 0
        # By identity: two copies could compare equal as dicts.
        self._queue = [row for row in self._queue if row is not entry]
        self._queue.append(entry)
        _logger.warning('battle %s refused %d times (HTTP %s); the other %d go first',
                        entry.get('arenaUniqueId'), failures, code, len(self._queue) - 1)
        save_queue(self._queue)

    def _retry(self, entry, why):
        """Keep this copy: the server does not have its battle yet.

        Nothing is sent again in this pass. Asking twice in the same second
        puts the same question to the same server and earns the same answer,
        so the bound would be spent before the battle had any chance to
        arrive. The next attempt comes from the reporter naming the battle,
        which schedules a collection within seconds of the results landing,
        and failing that from the next garage.

        With collection now waiting for that same confirmation this should be
        a dead branch. It is the net under it: a copy queued by an older
        version of this module, a confirmation that turned out to be about a
        row since removed.
        """
        tries = int(entry.get('tries') or 0) + 1
        # The entry is the queue's own dict, so this is the stored count.
        entry['tries'] = tries
        if tries >= _MAX_TRIES:
            forget(entry)
            self._queue = [row for row in self._queue
                           if row.get('arenaUniqueId') != entry.get('arenaUniqueId')]
            _logger.warning('battle %s was still %s after %d tries; dropping '
                            'its copy, %d waiting', entry.get('arenaUniqueId'),
                            why, tries, len(self._queue))
        else:
            # To the back. The queue is always drained from the front, so one
            # battle the server may never have would otherwise hold up every
            # copy behind it until its sixth try.
            self._queue.remove(entry)
            self._queue.append(entry)
            _logger.info('battle %s is not on the server yet (%s, try %d of '
                         '%d); its copy is kept', entry.get('arenaUniqueId'),
                         why, tries, _MAX_TRIES)
        save_queue(self._queue)
        self._done()

    def _drop_all(self, why):
        """Forget every pending file, and stop asking for a day."""
        _logger.info('the server keeps no replays (%s); dropping %d copy(ies)',
                     why, len(self._queue))
        for entry in self._queue:
            forget(entry)
        self._queue = []
        self._wanted = {}
        save_wanted(self._wanted)
        self._quiet_until = time.time() + _CLOSED_BACKOFF
        save_queue(self._queue)
        self._done()

    def _post(self):
        if not self._sends() or not self._queue:
            self._done()
            return
        entry = self._next()
        if entry is None:
            self._done()
            return
        try:
            with open(entry['file'], 'rb') as handle:
                data = handle.read()
        except (IOError, OSError):
            # Our own copy is gone. Nothing to send and nothing to keep.
            _logger.warning('the copy for battle %s is unreadable; dropping it',
                            entry.get('arenaUniqueId'))
            self._queue.pop(0)
            save_queue(self._queue)
            self._done()
            return

        body = json.dumps({
            'replay': base64.b64encode(data).decode('ascii'),
            'sha256': hashlib.sha256(data).hexdigest(),
        })

        def answered(response):
            code = getattr(response, 'responseCode', None)
            if code == 429:
                wait = _QUOTA_BACKOFF
                self._quiet_until = time.time() + wait
                self._done()
                _logger.warning('rate limited on replays; quiet for %.0fs', wait)
                return
            if code != 200:
                # Kept. A network that refused one upload is one the next
                # garage retries, and 422 (the body arrived damaged) is
                # precisely a case where the copy on disk is still good.
                self._done()
                # The code alone does not say which refusal it is: 400 is
                # both a missing `startedAt` and a body the server could not
                # read, and the two are fixed in different places.
                said = getattr(response, 'body', None)
                _logger.warning('replay upload stopped on HTTP %s for battle %s '
                                '(%d KB), %d still waiting; said %r',
                                code, entry.get('arenaUniqueId'), len(data) // 1024,
                                len(self._queue),
                                said[:200] if isinstance(said, basestring) else said)
                self._set_aside(entry, code)
                return
            answer = read_answer(getattr(response, 'body', None))
            if answer is None:
                self._done()
                _logger.warning('the server answered 200 with something that is not '
                                'a replay result; %d copy(ies) kept', len(self._queue))
                return
            kind, detail = answer
            if kind == 'refused' and detail in self.CLOSED:
                self._drop_all(detail)
                return
            if kind == 'refused' and detail in self.PENDING:
                self._retry(entry, detail)
                return
            if kind == 'refused' and detail in self.RETAKE:
                self._wrong_copy(entry)
                return
            if kind == 'stored' or detail in self.FINAL:
                forget(entry)
                self._queue = [row for row in self._queue
                               if row.get('arenaUniqueId') != entry.get('arenaUniqueId')]
                save_queue(self._queue)
                _logger.info('replay for battle %s %s, %d waiting',
                             entry.get('arenaUniqueId'),
                             'stored' if kind == 'stored' else detail,
                             len(self._queue))
                if self._queue:
                    self._post()
                else:
                    self._done()
                return
            # A refusal we have not met. Kept rather than guessed at.
            self._done()
            _logger.warning('replay for battle %s refused with an unknown reason (%s); kept',
                            entry.get('arenaUniqueId'), detail)

        self._last_flush = time.time()
        url = '%s/api/game/battles/%s/replay?startedAt=%d' % (
            config.API_BASE.rstrip('/'), entry['arenaUniqueId'],
            int(entry.get('startedAt') or 0))
        self._request(url, answered, body)

    def _request(self, url, answered, body):
        """Signed the same two ways the battle upload is signed."""
        secret = getattr(self._link, 'secret', None)
        if secret:
            self._fetch(url, answered, {'Authorization': 'Bearer %s' % secret}, body)
            return

        def with_token(response):
            if not (response and response.isValid()):
                _logger.info('no web token; replays wait for the next garage')
                self._done()
                return
            self._fetch(url, answered, {
                'X-Wargaming-Token': str(response.getToken()),
                'X-Wargaming-Region': config.REGION,
            }, body)

        try:
            from constants import TOKEN_TYPE
            from gui.shared.utils.requesters import getTokenRequester
            requester = getTokenRequester(TOKEN_TYPE.WGNI)
            if requester.isInProcess():
                self._done()
                return
            requester.request(timeout=10.0)(with_token)
        except Exception:
            _logger.exception('could not ask for a web token')
            self._done()

    def _fetch(self, url, answered, headers, body):
        headers = dict(headers)
        headers['Content-Type'] = 'application/json'
        # Longer than the API default: this body is 1.7 MB where every other
        # call the mod makes is a few KB, and a timeout here costs a replay
        # that no retry can recreate once the client has overwritten it.
        self._session.fetch(url, answered, headers=headers,
                            timeout=max(config.API_TIMEOUT, 60.0),
                            method='POST', post_data=body)


def install(session, settings, link):
    uploader = Uploader(session, settings, link)
    uploader.install()
    return uploader
