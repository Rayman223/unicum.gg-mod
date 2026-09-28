"""How the player has set their vehicles up, sent to unicum.gg.

Wargaming publishes none of this about anyone: their API answers battles and
damage, never the equipment on a gun or the perks a commander trained. So a
loadout on a player's page exists only because that player runs this mod, and
this module is the whole of that path.

What it sends, and what proves it
---------------------------------
Only ever this account's own vehicles, and the account is PROVEN rather than
claimed. The client can mint a WGNI web token, the one the game uses for its
own shop and portal, without asking the player anything; unicum.gg hands it to
Wargaming, which answers with the account id it is bound to. A nickname in the
body would be a claim, and the value of a published loadout rests entirely on
nobody being able to invent builds under a good player's name.

Why it sends so little
----------------------
A full carousel is a few hundred vehicles and about a quarter of a megabyte,
of which almost nothing moves between two garage visits. So the mod keeps a
fingerprint per vehicle and sends only what changed, reconciled against what
the server says it already holds, which makes the steady state nearly free and
leaves the whole payload for the first upload alone.

Measured on a 209-vehicle garage: 0.15 seconds of client time to read the lot,
1231 bytes a vehicle. The reading is not the cost here, the sending is.

When it runs
------------
At the garage and nowhere else: never in battle, never while the client is
starting. A moment after the garage appears, and again whenever the player
changes something, which is the half that was missing at first. Hanging it on
the garage alone meant somebody who spent an hour rebuilding tanks sent
nothing until they next came back from a battle, so their own page kept
showing a build they had already replaced.
"""
import hashlib
import json
import logging
import os
import time

from unicum import config

_logger = logging.getLogger('unicum.loadouts')

# Vehicles per pass, so a sweep never holds the frame the garage is drawn on.
_CHUNK = 25
_CHUNK_PAUSE = 0.1

# Vehicles per request. The endpoint accepts far more, but a request carrying
# a whole carousel is a quarter of a megabyte on a connection we know nothing
# about, and a failure throws all of it away rather than a fifth of it.
_BATCH = 50

# How long after the garage appears the sweep starts. Long enough for the
# sign-in, the carousel and the rest of this mod to have drawn: none of this
# is urgent.
_START_DELAY = 8.0

# How long after the player last changed something the sweep runs.
#
# A trailing delay rather than an immediate sweep, because mounting a piece of
# equipment is three or four syncs in a row and remounting a whole tank is a
# dozen. Waiting for the flurry to stop turns all of it into one upload, and
# twenty seconds is still fast enough that a player who tabs out to their own
# page finds it changed.
_CHANGE_DELAY = 20.0

# The least time between two sweeps.
#
# Four minutes rather than the quarter of an hour this was, because that
# quarter of an hour was the whole bug: a player who sat in the garage
# rebuilding tanks saw nothing reach their page until they next left it. A
# sweep with nothing to send costs no request at all (the fingerprints settle
# it locally), so what this really bounds is how often a CHANGE is uploaded,
# and fifteen an hour sits comfortably under the twenty the endpoint allows.
_MIN_INTERVAL = 240.0

# Which fingerprints were last accepted, so a second sweep sends nothing.
STORE = os.path.join('mods', 'configs', 'unicum', 'loadouts.json')

# Two setup groups, from the client's own post_progression_common: shells
# travel with consumables, optional devices with directives. Each has its own
# active index and at most two layouts.
_AMMO = 1
_DEVICES = 2


def _names(items):
    return [item.name if item is not None else None for item in items]


def _shells(collection):
    """Every shell of one setup, with how many the player loads."""
    shells = []
    for shell in collection:
        if shell is None:
            continue
        shells.append({
            # intCD as the key: a shell's name is scoped to its nation and two
            # nations can hold the same one. The name rides along as a label.
            'id': shell.intCD,
            'name': shell.name,
            # What an aggregate actually asks of this: how much gold does the
            # player carry. Derivable from the gun, but the client has it here.
            'type': shell.type,
            'premium': bool(shell.isPremium),
            'count': shell.count,
        })
    return shells


def _setup_group(vehicle, group):
    """{'active': index, 'layouts': [...]} for one of the client's two groups."""
    if group == _AMMO:
        first, second = vehicle.shells, vehicle.consumables
        keys = ('shells', 'consumables')
    else:
        first, second = vehicle.optDevices, vehicle.battleBoosters
        keys = ('optDevices', 'boosters')
    layouts = []
    for index in sorted(first.setupLayouts.setups):
        layouts.append({
            keys[0]: (_shells(first.setupLayouts.setupByIndex(index) or ())
                      if group == _AMMO else
                      _names(first.setupLayouts.setupByIndex(index) or ())),
            keys[1]: _names(second.setupLayouts.setupByIndex(index) or ()),
        })
    return {'active': first.setupLayouts.layoutIndex, 'layouts': layouts}


def _skills(tankman):
    """Every perk this tankman carries, taught or granted."""
    names = [skill.name for skill in tankman.skills]
    for bonus in tankman.bonusSkills.values():
        names.extend(skill.name for skill in bonus if skill is not None)
    return names


def _returning(vehicle, items=None):
    """{role: [skills, ...]} of the crew that last FOUGHT this vehicle.

    A player runs one crew across several vehicles: the commander comes off
    the IS-7 to drive the Leopard, and the IS-7 sits there with empty seats.
    Those seats are not how the IS-7 is played, and reading them as an absent
    crew is the crew half of the demount problem.

    The client already keeps the answer. `vehicle.lastCrew` holds the
    inventory ids of the crew that took this vehicle into battle last: it is
    what the garage's own "return crew" button and its auto-return read, and
    the game offers neither until the vehicle has fought, so the list means
    "last used" rather than "last mounted". A tankman sitting in another
    vehicle still resolves out of the inventory, so his perks are readable
    from here even while he is away.

    Grouped by role rather than by seat because `lastCrew` carries no slot
    index, and a role names a seat well enough: a second loader is a loader.
    """
    last = getattr(vehicle, 'lastCrew', None)
    if not last:
        return {}
    if items is None:
        items = inventory()
    if items is None:
        return {}
    waiting = {}
    for invID in last:
        tankman = items.getTankman(invID)
        # Dismissed, or gone from the inventory altogether: a crew the player
        # disbanded is genuinely absent, and saying so is the point.
        if tankman is None or tankman.isDismissed:
            continue
        waiting.setdefault(tankman.role, []).append(_skills(tankman))
    return waiting


def _crew(vehicle, items=None):
    """Skills per crew member, in the site's member order. Skills only.

    Nothing else about the member: a level and a training percentage describe
    the tankman rather than the build, and a column nobody reads is a column
    that still has to be kept true.

    An empty seat is filled from `_returning`, so a vehicle whose crew is off
    driving another one shows the crew it is played with rather than nothing.
    """
    from unicum.build import crew_member_indexes
    roles = vehicle.descriptor.type.crewRoles
    indexes = crew_member_indexes(roles)
    members = {}
    empty = []
    for slot, tankman in vehicle.crew:
        if slot not in indexes:
            continue
        if tankman is None:
            empty.append(slot)
            continue
        members[indexes[slot]] = {'role': roles[slot][0], 'skills': _skills(tankman)}
    if empty:
        # Whoever is actually sitting there wins: a crew put in since the last
        # battle is the current build, and `lastCrew` is then out of date.
        waiting = _returning(vehicle, items)
        for slot in empty:
            role = roles[slot][0]
            away = waiting.get(role)
            if away:
                members[indexes[slot]] = {'role': role, 'skills': away.pop(0)}
    return [members[key] for key in sorted(members)]


def _progression(vehicle):
    """Field modifications, or the skill tree a tier XI vehicle has instead."""
    from unicum.build import _PAIR_SIDES
    progression = vehicle.postProgression
    if progression is None or not progression.isExists():
        return None
    steps = [step for step in progression.iterUnorderedSteps() if step.isReceived()]
    if not steps:
        return None
    if progression.isVehSkillTree():
        return {'tree': sorted(step.stepID for step in steps)}
    pairs = []
    for step in steps:
        action = step.action
        if action.isMultiAction() and action.isPurchased():
            side = _PAIR_SIDES.get(action.getPurchasedIdx())
            if side:
                pairs.append({'name': action.getTechName(), 'side': side})
    return {'level': max(step.getLevel() for step in steps), 'pairs': pairs}


def _modules(vehicle):
    parts = {'gun': vehicle.gun, 'engine': vehicle.engine,
             'chassis': vehicle.chassis, 'radio': vehicle.radio}
    if vehicle.hasTurrets:
        parts['turret'] = vehicle.turret
    return dict((key, {'id': item.intCD, 'name': item.name})
                for key, item in parts.items() if item is not None)


def inventory():
    """The client's item cache, or None.

    Resolved once for a whole sweep and handed down, rather than looked up
    per vehicle: a carousel is a few hundred vehicles and this runs on the
    thread that draws the garage, so a service lookup in that loop is a few
    hundred lookups nobody asked for.
    """
    try:
        from helpers import dependency
        from skeletons.gui.shared import IItemsCache
        return dependency.instance(IItemsCache).items
    except Exception:
        _logger.exception('could not reach the inventory')
        return None


def loadout(vehicle, items=None):
    """One vehicle's whole setup, in the shape unicum.gg stores."""
    return {
        'tankId': vehicle.intCD,
        'modules': _modules(vehicle),
        'crew': _crew(vehicle, items),
        'progression': _progression(vehicle),
        'setups': {
            'ammo': _setup_group(vehicle, _AMMO),
            'devices': _setup_group(vehicle, _DEVICES),
        },
    }


def fingerprint(record):
    """What identifies this loadout, so an unchanged one is never sent twice."""
    return hashlib.md5(json.dumps(record, sort_keys=True)).hexdigest()


def mounted(record):
    """The optional devices bolted to this vehicle, as a set of names.

    The active setup only, and devices only. Consumables and directives are
    bought and spent rather than moved between vehicles, so they are not what
    the demount rule below is about.
    """
    devices = (record.get('setups') or {}).get('devices') or {}
    layouts = devices.get('layouts') or []
    if not layouts:
        return set()
    layout = layouts[devices.get('active', 0)] if devices.get('active', 0) < len(layouts) else layouts[0]
    return set(name for name in (layout.get('optDevices') or []) if name)


def crewed(record):
    """How many seats this record has somebody in."""
    return len(record.get('crew') or ())


def entry_of(value):
    """One store entry, whatever shape it arrives in.

    Two shapes reach this. A file written by an older build holds a bare
    fingerprint, with nothing said about what was mounted. And a reload can
    hand it the OTHER old shape from memory: a network callback outlives the
    generation that made it, so an answer still in flight calls whatever
    `save_sent` the module holds NOW with the `_sent` the previous one built.
    That is the orphan this mod has been bitten by before, and a serialiser is
    the worst place to meet it: it runs inside a fetch callback, where raising
    loses the whole store and reports an error the player can do nothing with.
    """
    if isinstance(value, dict):
        return {'f': value.get('f'), 'd': value.get('d'), 'c': value.get('c')}
    return {'f': value if isinstance(value, basestring) else None, 'd': None, 'c': None}


def _read_store():
    """The whole file, or an empty document.

    Read whole and written whole because it now carries two things that must
    not overwrite one another: the fingerprints, and the withdrawal a player
    has asked for and the server has not yet confirmed.
    """
    if not os.path.isfile(STORE):
        return {}
    try:
        with open(STORE, 'rb') as handle:
            stored = json.load(handle)
    except (IOError, ValueError):
        _logger.warning('could not read %s, sending everything', STORE)
        return {}
    return stored if isinstance(stored, dict) else {}


def _write_store(document):
    try:
        directory = os.path.dirname(STORE)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(STORE, 'wb') as handle:
            json.dump(document, handle)
        return True
    except (IOError, OSError):
        _logger.warning('could not write %s; the next sweep sends again', STORE, exc_info=True)
        return False


def load_sent():
    """{tank id: {'f': fingerprint, 'd': set(devices), 'c': crew size}} the server took.

    The devices and the crew size ride along with the fingerprint because a
    fingerprint only says THAT a vehicle changed, and the two rules below have
    to know HOW.
    """
    sent = _read_store().get('sent')
    if not isinstance(sent, dict):
        return {}
    out = {}
    for key, value in sent.items():
        entry = entry_of(value)
        # An entry with no devices recorded is unprotected against a demount
        # until its next real change, which is the right price for reading an
        # older file rather than discarding the whole carousel. Same for a
        # crew size the file does not carry.
        out[int(key)] = {'f': entry['f'],
                         'd': set(entry['d']) if entry['d'] is not None else None,
                         'c': entry['c']}
    return out


def save_sent(sent):
    on_disk = {}
    for key, value in sent.items():
        entry = entry_of(value)
        on_disk[key] = {'f': entry['f'], 'd': sorted(entry['d'] or ()), 'c': entry['c']}
    document = _read_store()
    document['sent'] = on_disk
    _write_store(document)


def load_withdraw():
    """Whether the player asked us to be forgotten and the server has not said it did."""
    return bool(_read_store().get('withdraw'))


def save_withdraw(pending):
    """Remember, across restarts, that a withdrawal is still owed.

    A player unticks the box and closes the game, or the request fails
    because the network is down. Either way the rows are still published and
    the player believes they are not, so the ask has to outlive the session
    that made it and be repeated until the server confirms.
    """
    document = _read_store()
    if pending:
        document['withdraw'] = True
    else:
        document.pop('withdraw', None)
    _write_store(document)


def _keep_rejected(code, batch):
    """Leave a refused batch on disk, beside the fingerprints.

    Kept rather than removed once it had done its job. The shape of a loadout
    is a contract between this mod, a game client nobody here controls and a
    server in another repository, and the two bugs it has already found were
    both a vehicle the schema had not imagined: one with nothing to say about
    its post progression, and one whose main armament is a machine gun
    carrying 2700 rounds. Neither was visible from the log, and neither could
    be reproduced without the batch that carried it.

    Only what the server refused outright, never a network failure, and always
    the same file: this is a diagnosis, not a history.
    """
    if not (400 <= (code or 0) < 500):
        return
    try:
        with open(os.path.join('mods', 'configs', 'unicum', 'loadouts-rejected.json'), 'wb') as handle:
            json.dump(batch, handle)
    except (IOError, OSError):
        _logger.debug('could not write the refused batch down', exc_info=True)


def demounted(record, previous):
    """Whether this is a vehicle being stripped rather than rebuilt.

    A player moves equipment between vehicles constantly: the stabiliser comes
    off the IS-7 to go on the Leopard, and for a while the IS-7 sits there with
    an empty slot. That empty slot is not how the IS-7 is played, and recording
    it puts a build on the player's page that they have never used. Worse, the
    vehicle most worth reading about is often the one just stripped to equip
    the next.

    So a strict SUBSET is the signature: every device still mounted was already
    mounted before, and there are fewer of them. Swapping a rammer for vents is
    not a subset and is recorded, because that is a real decision. Only the
    taking away is refused, and a battle undoes the refusal (see `_on_battle`),
    since a vehicle that goes to war is equipped the way its owner means to
    play it.
    """
    before = previous.get('d') if previous else None
    if not before:
        # Nothing known about what was mounted (a store written before this
        # rule existed), so there is nothing to protect.
        return False
    now = mounted(record)
    return now < before


def uncrewed(record, previous):
    """Whether this is a vehicle whose crew is away rather than disbanded.

    The crew half of `demounted`, and the residue of it: `_crew` already
    fills the empty seats of a vehicle that has fought from its last crew, so
    what still reaches here is a vehicle the client keeps no last crew for,
    one that has never been to battle since it was built.

    A count is the whole signature. A vehicle never loses a seat, so fewer
    members than we recorded can only mean the crew went to drive something
    else. Retraining or resetting a perk keeps the members, so it is recorded
    rather than refused, which is right: that one is a decision.
    """
    before = previous.get('c') if previous else None
    if not before:
        # Nothing known about the crew (a store written before this rule
        # existed), so there is nothing to protect.
        return False
    return crewed(record) < before


def changed(records, sent, held):
    """The records worth sending: new, altered, or absent from the server.

    `held` is what the server says it already has. A fingerprint alone is not
    enough to decide: this file survives a reinstall of the site's database,
    and a vehicle we believe we sent but that the server does not hold would
    otherwise never be sent again.
    """
    out = []
    for record in records:
        tank = record['tankId']
        previous = sent.get(tank)
        known = previous.get('f') if previous else None
        if known == fingerprint(record) and (held is None or tank in held):
            continue
        if demounted(record, previous) or uncrewed(record, previous):
            continue
        out.append(record)
    return out


class Uploader(object):
    """Reads the carousel when the garage appears, and sends what changed.

    One object for the whole path, because the three halves only make sense
    together: a sweep that does not know what was already sent would upload a
    quarter of a megabyte on every garage entry, and a sender that does not
    know when the sweep finished would send half a carousel.
    """

    def __init__(self, session, settings, link):
        self._session = session
        self._settings = settings
        self._link = link
        self._sent = load_sent()
        # What the server says it holds, read once a session before the first
        # upload. None until then, which `changed` reads as "do not second
        # guess the fingerprints".
        self._held = None
        self._last_sweep = 0.0
        self._busy = False
        # When the player last touched a vehicle, for the trailing delay.
        self._changed_at = 0.0
        # What the setting said last time we looked, so that turning it OFF is
        # a moment we can act on rather than a state we merely obey.
        self._sharing = self._shares()
        # Here rather than in `install`, which returns early when the client
        # has no player events: a mod that cannot upload anything must still
        # honour a player asking to be forgotten for what an earlier session
        # uploaded.
        self._settings.on_change(self._on_setting)

    def install(self):
        try:
            from PlayerEvents import g_playerEvents
            self._session.subscribe(g_playerEvents.onAccountShowGUI, self._on_garage)
            # Joining the queue, which is the last moment the lobby still has
            # the vehicle and the first at which it is certainly equipped.
            self._session.subscribe(g_playerEvents.onEnqueued, self._on_battle)
        except ImportError:
            _logger.exception('no player events; loadouts are never swept')
            return
        self._follow_changes()
        # The garage may already be up: the event fires when it appears, and
        # a mod installed after that would otherwise say nothing until the
        # player next came back from a battle. `sweep` refuses anywhere else,
        # so this costs nothing while the client is still starting, which is
        # when it normally runs.
        self._session.callback(_START_DELAY, self.sweep)
        # A withdrawal asked for in a previous session, which the game was
        # closed before we could deliver. `_on_garage` catches the later ones.
        if load_withdraw():
            self._session.callback(_START_DELAY, self._withdraw)
        _logger.info('installed')

    def _follow_changes(self):
        """Sweep when the player rebuilds a tank, not only when they leave.

        The garage event alone was the bug this fixes: it fires when the
        garage APPEARS, so a player who spends an hour there remounting
        vehicles sent nothing until they next came back from a battle. Their
        own page kept showing a build they had already replaced, which is
        exactly the thing a page like that must not do.

        `onSyncCompleted` is the client telling us its inventory changed, and
        it is what the game's own screens listen to for the same reason. It
        fires for far more than a loadout (a purchase, a battle result), which
        costs nothing here: a sweep that finds nothing changed sends nothing.
        """
        try:
            from helpers import dependency
            from skeletons.gui.shared import IItemsCache
            self._session.subscribe(dependency.instance(IItemsCache).onSyncCompleted,
                                    self._on_change)
        except Exception:
            _logger.exception('no items cache; loadouts only sweep on the garage')

    def _on_change(self, *args):
        # Trailing: remounting a tank is a flurry of syncs, and each one
        # pushes the moment back rather than starting its own sweep.
        self._changed_at = time.time()
        self._session.callback(_CHANGE_DELAY, self._sweep_if_settled)

    def _sweep_if_settled(self):
        """Sweep once the flurry has stopped, else let the later one do it."""
        if time.time() + 0.5 < self._changed_at + _CHANGE_DELAY:
            return
        self.sweep()

    def _on_battle(self, *args):
        """Record the vehicle going to battle, whatever the garage says.

        This is what "the last loadout used" means, and it is the only moment
        the game guarantees it: a vehicle cannot enter a battle half stripped,
        so what it carries here is what its owner means to play it with. It
        therefore overrides the demount rule, which exists precisely to keep a
        half-stripped garage state off the page until this happens.

        Sent on its own rather than through a sweep: it is one vehicle, the
        moment is brief, and the player is about to leave the garage.
        """
        if self._busy or not self._wanted():
            return
        vehicle = self._selected()
        if vehicle is None:
            return
        try:
            record = loadout(vehicle)
        except Exception:
            _logger.exception('could not read the loadout of the vehicle going to battle')
            return
        previous = self._sent.get(record['tankId'])
        if previous and previous.get('f') == fingerprint(record):
            return
        _logger.info('sending the loadout of the vehicle going to battle')
        self._busy = True
        self._post([record], [])

    @staticmethod
    def _selected():
        """The vehicle the player is about to take out, or None."""
        try:
            from CurrentVehicle import g_currentVehicle
            return g_currentVehicle.item if g_currentVehicle.isPresent() else None
        except Exception:
            _logger.exception('could not read the selected vehicle')
            return None

    def _shares(self):
        """What the setting says, on its own: no garage, no account, just the box."""
        try:
            return self._settings.sends_loadouts()
        except Exception:
            _logger.exception('could not read the loadout setting')
            return False

    def _on_setting(self):
        """Unticking the box is a withdrawal, not merely a pause.

        Stopping the uploads was all this used to do, which left everything
        already sent on the player's page for good. Someone who unticks a box
        that says they share their configurations has not asked us to freeze
        their page, they have asked to be off it.

        Only the falling edge. The setting is notified on every change of any
        setting, so the previous value is what makes this a moment.
        """
        sharing = self._shares()
        if self._sharing and not sharing:
            _logger.info('loadout sharing turned off; asking the server to forget this account')
            save_withdraw(True)
            self._withdraw()
        self._sharing = sharing

    def _withdraw(self):
        """Ask the server to forget this account, and keep asking until it has.

        The pending flag outlives the attempt and the session: a refusal here
        leaves rows published that the player believes are gone, so this is
        the one request in the file that must not be allowed to fail quietly.
        """

        def answered(response):
            code = getattr(response, 'responseCode', None)
            if code != 200:
                _logger.warning('the server would not forget this account (HTTP %s); '
                                'asked again at the next garage', code)
                return
            # Both halves, or a re-tick would upload nothing: the fingerprints
            # say the server already holds a carousel it has just dropped.
            self._sent = {}
            self._held = None
            save_sent({})
            save_withdraw(False)
            _logger.info('the server forgot this account')

        self._request('%s/api/game/loadouts' % config.API_BASE.rstrip('/'), answered,
                      method='DELETE')

    def _on_garage(self, *args):
        # A withdrawal the player asked for in a previous session, or one the
        # network refused. It comes first: it is the only thing here that is
        # owed to somebody.
        if load_withdraw():
            self._session.callback(_START_DELAY, self._withdraw)
        # Not on the spot: the garage has a sign-in, a carousel and every
        # other feature of this mod to draw first, and none of this is urgent.
        self._session.callback(_START_DELAY, self.sweep)

    def sweep(self):
        """Read every owned vehicle, then send what the server does not have."""
        if self._busy or not self._wanted():
            return
        now = time.time()
        if now - self._last_sweep < _MIN_INTERVAL:
            return
        vehicles = self._vehicles()
        if not vehicles:
            return
        self._busy = True
        self._last_sweep = now
        _Sweep(self, vehicles).step()

    def _wanted(self):
        """Whether the player is at the garage and has not turned this off."""
        try:
            from helpers import isPlayerAccount
            if not isPlayerAccount():
                return False
        except ImportError:
            return False
        try:
            return self._settings.sends_loadouts()
        except Exception:
            _logger.exception('could not read the loadout setting')
            return False

    @staticmethod
    def _vehicles():
        try:
            from gui.shared.utils.requesters import REQ_CRITERIA
            from helpers import dependency
            from skeletons.gui.shared import IItemsCache
            items = dependency.instance(IItemsCache).items
            return list(items.getVehicles(REQ_CRITERIA.INVENTORY).values())
        except Exception:
            _logger.exception('could not list the vehicles')
            return []

    def later(self, delay, func):
        """Schedule the sweep's next chunk on this session's own clock."""
        self._session.callback(delay, func)

    def swept(self, records):
        """Every vehicle read. Reconcile with the server, then send."""
        if self._held is None:
            self._read_held(lambda: self._send(records))
        else:
            self._send(records)

    def _send(self, records):
        self._learn(records)
        pending = changed(records, self._sent, self._held)
        # A vehicle the player parted with: we hold a fingerprint for it and
        # it is no longer in the carousel.
        owned = set(record['tankId'] for record in records)
        sold = [tank for tank in self._sent if tank not in owned]
        if not pending and not sold:
            self._busy = False
            _logger.info('nothing to send: %d vehicles, all of them unchanged', len(records))
            return
        _logger.info('sending %d of %d vehicles, and %d sold', len(pending), len(records), len(sold))
        self._post(pending, sold)

    def _learn(self, records):
        """Fill in what vehicles we only hold a fingerprint for were carrying.

        Without this neither the demount rule nor the crew rule would protect
        a single vehicle a player already owns: a store written by an earlier
        build says what was sent but not what was on it, and a vehicle would
        only be protected once it changes, which is exactly the moment the
        protection was meant to cover.

        Safe because the fingerprint is what decides: if it still matches, the
        vehicle in the carousel IS the one we sent, so what it carries now is
        what it carried then.
        """
        learned = 0
        for record in records:
            entry = self._sent.get(record['tankId'])
            if not entry or (entry.get('d') is not None and entry.get('c') is not None):
                continue
            if entry.get('f') != fingerprint(record):
                continue
            if entry.get('d') is None:
                entry['d'] = mounted(record)
            if entry.get('c') is None:
                entry['c'] = crewed(record)
            learned += 1
        if learned:
            _logger.info('learned what %d vehicle(s) sent by an earlier build were carrying',
                         learned)
            save_sent(self._sent)

    def _post(self, pending, sold, at=0):
        """One batch, then the next. Stops at the first failure."""
        batch = pending[at:at + _BATCH]
        if not batch and (at > 0 or not sold):
            self._busy = False
            save_sent(self._sent)
            return
        body = json.dumps({'loadouts': batch, 'sold': sold if at == 0 else []})

        def answered(response):
            code = getattr(response, 'responseCode', None)
            if code != 200:
                # Stop rather than push on: a quota answer (429) means the
                # rest of the batches would be refused too, and any other
                # failure is as likely to hit them. What was accepted is
                # remembered, so the next sweep carries on where this left.
                self._busy = False
                save_sent(self._sent)
                _logger.warning('loadout upload stopped on HTTP %s after %d vehicle(s)', code, at)
                _keep_rejected(code, batch)
                return
            for record in batch:
                self._sent[record['tankId']] = {'f': fingerprint(record),
                                                'd': mounted(record),
                                                'c': crewed(record)}
            if at == 0:
                for tank in sold:
                    self._sent.pop(tank, None)
            self._post(pending, sold, at + _BATCH)

        self._request('%s/api/game/loadouts' % config.API_BASE.rstrip('/'), answered,
                      method='POST', post_data=body)

    def _read_held(self, then):
        """What the server already holds, so a lost database is refilled."""

        def answered(response):
            payload = None
            if getattr(response, 'responseCode', None) == 200:
                try:
                    payload = json.loads(response.body)
                except (TypeError, ValueError):
                    payload = None
            tanks = payload.get('tanks') if isinstance(payload, dict) else None
            self._held = set(int(key) for key in tanks) if isinstance(tanks, dict) else set()
            then()

        self._request('%s/api/game/loadouts' % config.API_BASE.rstrip('/'), answered)

    def _request(self, url, answered, method='GET', post_data=''):
        """Signed with whatever proves this account, and nothing if neither does.

        The link's secret when the player has one, because it costs the server
        no call to Wargaming. Otherwise the client's own WGNI web token, which
        the game mints without asking the player anything and which unicum.gg
        has Wargaming confirm. Either way the account is proven rather than
        claimed: a nickname in the body would let anyone publish a loadout
        under somebody else's name, and the whole value of this rests on that
        being impossible.
        """
        secret = getattr(self._link, 'secret', None)
        if secret:
            self._fetch(url, answered, {'Authorization': 'Bearer %s' % secret},
                        method, post_data)
            return

        def with_token(response):
            if not (response and response.isValid()):
                _logger.info('no web token; loadouts wait for the next garage')
                self._busy = False
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
                self._busy = False
                return
            requester.request(timeout=10.0)(with_token)
        except Exception:
            _logger.exception('could not ask for a web token')
            self._busy = False

    def _fetch(self, url, answered, headers, method, post_data):
        headers = dict(headers)
        headers['Content-Type'] = 'application/json'
        self._session.fetch(url, answered, headers=headers,
                            timeout=config.API_TIMEOUT, method=method,
                            post_data=post_data)


class _Sweep(object):
    """One pass over the carousel, a chunk at a time.

    Chunked because the client draws the garage on the thread this runs on.
    A few hundred vehicles is 0.15 seconds of work measured end to end, which
    is not much and is still a dropped frame if it lands in one go.
    """

    def __init__(self, uploader, vehicles):
        self._uploader = uploader
        self._left = vehicles
        self._records = []
        self._items = inventory()

    def step(self):
        chunk, self._left = self._left[:_CHUNK], self._left[_CHUNK:]
        for vehicle in chunk:
            try:
                self._records.append(loadout(vehicle, self._items))
            except Exception:
                # One unreadable vehicle is not worth losing the carousel
                # over, and a client patch is exactly how one appears.
                _logger.exception('could not read the loadout of %s',
                                  getattr(vehicle, 'name', '?'))
        if self._left:
            self._uploader.later(_CHUNK_PAUSE, self.step)
        else:
            self._uploader.swept(self._records)


def install(session, settings, link):
    uploader = Uploader(session, settings, link)
    uploader.install()
    return uploader
