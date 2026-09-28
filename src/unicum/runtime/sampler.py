"""What the game's main thread is actually running, mod by mod.

`perf` measures this mod, because it can: everything this mod does passes
through a `Session`. Nothing passes through a `Session` in the other
twenty-eight mods a player has installed, or in the client itself, so a
player whose frame rate halved gets told our share and nothing else. That is
half an answer, and the half that matters least.

This is the other half, and it needs no cooperation from anybody. A daemon
thread wakes a hundred times a second and reads the main thread's Python
stack with `sys._current_frames()`, which the GIL makes possible: while the
main thread runs Python, the sampler can look at it. Counting where it was
found, over thousands of samples, is a profile.

Two things make the result worth reading:

  - Every sample is attributed to whatever owns the file it was found in:
    a .wotmod, a res_mods script, this package, or the client. So the answer
    is a name a player recognises, not a stack trace.
  - A main thread found with NO Python frame is counted too, under "the
    engine". That line is the most important one on the table: the client is
    C++ and draws in C++, so a profile where it dominates says the mods are
    not the problem, however slow the game feels. Without it, a table of
    Python functions would imply Python was the cost.

What it cannot do is see inside C++. It says how much time is spent there,
never on what.
"""
import logging
import os
import sys
import threading
import time

_logger = logging.getLogger('unicum.sampler')

# A hundred samples a second: fine enough that a routine costing a whole
# frame is caught several times, cheap enough that the sampler itself is
# noise. Each sample is a dictionary lookup and a short walk up the stack.
_HZ = 100.0

# How deep to walk before giving up. Guards against a pathological stack
# rather than any real one.
_MAX_DEPTH = 120


def _owner(filename):
    """Who a Python file belongs to, in the words a player would use.

    Filenames the client reports come in three shapes: a path inside a
    .wotmod archive, a loose script under res_mods, and the client's own
    scripts. The archive's own name is the useful one, because that is what
    a player installed, removes and reports.
    """
    if not filename:
        return 'the client'
    path = filename.replace('\\', '/').lower()

    # A .wotmod is a zip the client mounts: its own name appears in the path
    # of everything loaded out of it.
    marker = '.wotmod'
    at = path.find(marker)
    if at != -1:
        return os.path.basename(path[:at + len(marker)])

    # This package, however it was loaded: the dev bootstrap reports a path
    # in the working copy, a release build a path inside its archive.
    if '/unicum/' in path or path.endswith('/unicum.py'):
        return 'unicum.gg (this mod)'

    # A loose script, which is how most mods are installed by a modpack.
    at = path.find('/res_mods/')
    if at != -1:
        rest = path[at + len('/res_mods/'):]
        parts = [p for p in rest.split('/') if p]
        # Skip the client version folder, then name the script itself: a
        # loose mod is one file, and that file is its name.
        return 'res_mods/%s' % (parts[-1] if parts else rest)

    return 'the client'


class Sampler(object):
    """Samples the main thread while it is asked to."""

    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self._main = None
        self._lock = threading.Lock()
        # {owner: count} and {(owner, where): count}
        self._owners = {}
        self._places = {}
        self._samples = 0
        self._idle = 0
        self._started = 0.0

    def start(self, main_thread_id):
        """Begin sampling the thread whose id this is. Call from that thread."""
        self.clear()
        self._main = main_thread_id
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run)
        # Daemon, or a client trying to close would wait on us. There is
        # nothing here worth delaying a shutdown for.
        self._thread.daemon = True
        self._thread.name = 'unicum-sampler'
        self._thread.start()
        _logger.info('sampling the main thread at %d Hz', int(_HZ))

    def stop(self):
        self._stop.set()
        self._thread = None

    def running(self):
        return self._thread is not None and not self._stop.is_set()

    def clear(self):
        with self._lock:
            self._owners = {}
            self._places = {}
            self._samples = 0
            self._idle = 0
            self._started = time.time()

    def _run(self):
        period = 1.0 / _HZ
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception:
                # A sampler that kills itself on one bad frame is worse than
                # useless: it goes quiet and looks like an answer.
                _logger.debug('a sample failed', exc_info=True)
            time.sleep(period)

    def _sample(self):
        frames = sys._current_frames()
        frame = frames.get(self._main)
        with self._lock:
            self._samples += 1
            if frame is None:
                # The main thread holds no Python frame: it is inside the
                # engine, which is where a client spends most of its time.
                self._idle += 1
                return
            # The leaf says what is running; the owner is read from the
            # DEEPEST frame that belongs to somebody, so that a mod calling
            # into a client helper is still charged to the mod.
            leaf = frame
            owner = _owner(leaf.f_code.co_filename)
            where = '%s:%s' % (os.path.basename(
                leaf.f_code.co_filename.replace('\\', '/')), leaf.f_code.co_name)
            walked, up = 0, frame
            while up is not None and walked < _MAX_DEPTH:
                found = _owner(up.f_code.co_filename)
                if found != 'the client':
                    owner = found
                up = up.f_back
                walked += 1
            self._owners[owner] = self._owners.get(owner, 0) + 1
            key = (owner, where)
            self._places[key] = self._places.get(key, 0) + 1

    def rows(self):
        """(owners worst first, places worst first, samples, idle, seconds)."""
        with self._lock:
            owners = sorted(self._owners.items(), key=lambda kv: kv[1], reverse=True)
            places = sorted(self._places.items(), key=lambda kv: kv[1], reverse=True)
            return owners, places, self._samples, self._idle, max(time.time() - self._started, 1e-6)

    def report(self, limit=10):
        owners, places, samples, idle, elapsed = self.rows()
        if not samples:
            _logger.info('no samples of the main thread yet')
            return
        busy = samples - idle
        _logger.info('main thread over %.1f s: %.1f%% inside the engine (C++), '
                     '%.1f%% running Python, from %d samples',
                     elapsed, 100.0 * idle / samples, 100.0 * busy / samples, samples)
        if not busy:
            _logger.info('  no Python ran on the main thread at all: no mod is costing you frames')
            return
        _logger.info('  %-44s %8s %9s', 'whose code', 'samples', '% of all')
        for owner, count in owners[:limit]:
            _logger.info('  %-44s %8d %8.2f%%', owner[:44], count, 100.0 * count / samples)
        _logger.info('  and where, exactly:')
        for (owner, where), count in places[:limit]:
            _logger.info('  %-30s %-28s %6.2f%%', owner[:30], where[:28], 100.0 * count / samples)


# One per load. The session tears it down, so a reload cannot leave a second
# thread sampling on behalf of code that no longer exists.
_sampler = Sampler()


def start():
    import thread
    _sampler.start(thread.get_ident())


def stop():
    _sampler.stop()


def clear():
    _sampler.clear()


def report(limit=10):
    _sampler.report(limit)


def rows():
    return _sampler.rows()


def running():
    return _sampler.running()
