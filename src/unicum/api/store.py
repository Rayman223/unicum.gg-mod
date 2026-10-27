"""The lookup's answers on disk, between sessions.

Its job is the first paint: answers come over the network but are consumed
synchronously, so without it every game start draws one unmarked contacts
list before they arrive. Entries keep the time they were fetched, so old ones
are drawn at once and refreshed in the background rather than trusted for
another half hour.
"""
import json
import logging
import os
import time

from unicum.api.entry import CLANS, PLAYERS, TAGS, Entry

_logger = logging.getLogger('unicum.api')

_VERSION = 2

# How much of the past is worth keeping. Nothing used to be dropped, so the
# file held every player ever seen -- 11,000 of them, 5.2 MB, 39 days -- and
# every save rewrote all of it, which is how a cache meant to speed up the
# first paint turned into a one-second freeze that grew every week.
#
# A week is generous for what an entry is actually worth. Anything older than
# `config.REFRESH_SECONDS` (half an hour) is refetched the moment it is seen
# again, so a month-old entry buys one stale frame of paint on the small
# chance that player turns up at all, and charges a share of every rewrite
# for it.
KEEP_SECONDS = 7 * 86400


def load(path, region):
    """({(kind, id): Entry}, {TAG: (clan id, fetched_at)}), empty if unusable."""
    cache, tags = {}, {}
    if not path or not os.path.isfile(path):
        return cache, tags
    try:
        with open(path, 'rb') as handle:
            stored = json.load(handle)
    except (IOError, ValueError):
        _logger.warning('could not read %s, starting cold', path)
        return cache, tags
    if stored.get('version') != _VERSION or stored.get('region') != region:
        return cache, tags
    for kind in (PLAYERS, CLANS):
        for key, raw in (stored.get(kind) or {}).items():
            try:
                cache[(kind, int(key))] = Entry.from_store(raw)
            except (TypeError, ValueError, AttributeError):
                continue
    for tag, raw in (stored.get(TAGS) or {}).items():
        try:
            tags[tag] = (int(raw['id']), float(raw['fetchedAt']))
        except (TypeError, ValueError, KeyError):
            continue
    _logger.info('warmed %s entries and %s tags from %s', len(cache), len(tags), path)
    return cache, tags


def save(path, region, cache, tags):
    """Write the known answers, minus the stale ones; True once on disk.

    Only the write forgets. The live cache keeps everything it learned for
    the session, because `Lookup.get` is called from inside the VO builders
    and must never answer None for something it answered a moment ago --
    dropping stale entries from memory once blanked every flag of a contacts
    list at the same time.
    """
    payload = {'version': _VERSION, 'region': region, PLAYERS: {}, CLANS: {}, TAGS: {}}
    cutoff = time.time() - KEEP_SECONDS
    for (kind, entity_id), entry in cache.items():
        # "Unknown" is not written down: the server may simply not hold the
        # account yet, and a stored miss would hide it for a day.
        if entry.known and entry.fetched_at >= cutoff:
            payload[kind][str(entity_id)] = entry.to_store()
    for tag, (clan_id, fetched_at) in tags.items():
        if clan_id and fetched_at >= cutoff:
            payload[TAGS][tag] = {'id': clan_id, 'fetchedAt': fetched_at}
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        # One string, then one write, rather than `json.dump(payload, handle)`.
        # They produce the same bytes, but dump() runs the encoder as a
        # generator and calls handle.write() per fragment, which for a file
        # this size is hundreds of thousands of writes: measured on a real
        # 5.2 MB cache under the client's own Python 2.7, 909 ms against
        # 163 ms. The game thread is blocked for the whole of it.
        blob = json.dumps(payload)
        with open(path, 'wb') as handle:
            handle.write(blob)
        return True
    except (IOError, OSError):
        _logger.exception('could not write %s', path)
        return False
