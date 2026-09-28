"""Checks for the loadouts the mod sends: what changes, and what is never sent twice."""

from checks.common import check


def check_loadouts():
    from unicum.loadouts import changed, fingerprint

    first = {'tankId': 7169, 'crew': [{'role': 'commander', 'skills': ['repair']}]}
    same = {'crew': [{'role': 'commander', 'skills': ['repair']}], 'tankId': 7169}
    other = {'tankId': 7169, 'crew': [{'role': 'commander', 'skills': ['brotherhood']}]}
    check('a loadout fingerprints the same whatever order its keys come in',
          fingerprint(first) == fingerprint(same))
    check('a changed skill changes the fingerprint', fingerprint(first) != fingerprint(other))

    # The shape `load_sent` hands out: a fingerprint and what was mounted.
    sent = {7169: {'f': fingerprint(first), 'd': set()}}
    check('an unchanged vehicle is not sent again',
          changed([first], sent, set([7169])) == [])
    check('a vehicle whose setup moved is sent',
          [r['tankId'] for r in changed([other], sent, set([7169]))] == [7169])
    check('a vehicle we have never sent is sent',
          [r['tankId'] for r in changed([first], {}, set())] == [7169])
    # The fingerprints outlive the server's own rows: this file sits in the
    # player's game folder and knows nothing of a database that was restored
    # from a backup, so a vehicle the server does not list is sent again even
    # though we believe we sent it.
    check('a vehicle the server no longer holds is sent again',
          [r['tankId'] for r in changed([first], sent, set())] == [7169])
    check('with no answer from the server, the fingerprints alone decide',
          changed([first], sent, None) == [])


def check_loadout_store():
    """The file survives a mod that did not write it in this shape."""
    import json
    import os
    import tempfile
    from unicum import loadouts

    folder = tempfile.mkdtemp()
    path = os.path.join(folder, 'loadouts.json')
    store, loadouts.STORE = loadouts.STORE, path
    try:
        check('no file means nothing was ever sent', loadouts.load_sent() == {})
        # Written by a build that kept fingerprints alone. Reading it must not
        # throw away the carousel: a vehicle is simply unprotected against a
        # demount until its next real change.
        with open(path, 'wb') as handle:
            json.dump({'sent': {'7169': 'oldfingerprint'}}, handle)
        old = loadouts.load_sent()
        check('a store from before the devices were kept is still read',
              old == {7169: {'f': 'oldfingerprint', 'd': None, 'c': None}})
        loadouts.save_sent({7169: {'f': 'abc', 'd': set(['rammer']), 'c': 5}})
        check('what a sweep saves comes back as it went in',
              loadouts.load_sent() == {7169: {'f': 'abc', 'd': set(['rammer']), 'c': 5}})
        # A network answer outlives the generation that sent it, so a reload
        # can hand the serialiser the shape the PREVIOUS build kept in memory.
        # Raising there runs inside a fetch callback: it loses the whole store
        # and reports an error the player can do nothing about. Seen live.
        loadouts.save_sent({7169: 'a bare fingerprint from before the reload'})
        back = loadouts.load_sent()
        check('a store handed the shape an older generation held is still written',
              back[7169]['f'] == 'a bare fingerprint from before the reload')
        # What matters is not which empty it round-trips to but what that
        # empty means: nothing is known about the devices, so nothing is
        # protected, and the vehicle is recorded again on its next change.
        check('and the vehicle it names is simply unprotected until it changes',
              not loadouts.demounted(_with_devices(7169, ['rammer']), back[7169]))
        check('unprotected against a crew that left, too',
              not loadouts.uncrewed({'tankId': 7169, 'crew': []}, back[7169]))
    finally:
        loadouts.STORE = store


def _with_devices(tank, names, extra=None):
    """A record carrying these optional devices in its active setup."""
    record = {
        'tankId': tank,
        'setups': {'devices': {'active': 0, 'layouts': [{'optDevices': list(names),
                                                         'boosters': []}]}},
    }
    if extra:
        record.update(extra)
    return record


def check_demounting():
    """A vehicle stripped to equip another must not lose its build on the page."""
    from unicum.loadouts import changed, fingerprint, mounted

    full = _with_devices(7169, ['rammer', 'vents', 'stabilizer'])
    stripped = _with_devices(7169, ['rammer', 'vents', None])
    swapped = _with_devices(7169, ['rammer', 'vents', 'optics'])
    emptied = _with_devices(7169, [None, None, None])

    check('the devices of a setup are read off the active layout',
          mounted(full) == set(['rammer', 'vents', 'stabilizer']))
    check('an empty slot is not a device', mounted(stripped) == set(['rammer', 'vents']))

    sent = {7169: {'f': fingerprint(full), 'd': mounted(full)}}
    check('a vehicle stripped of one device keeps the build it was played with',
          changed([stripped], sent, set([7169])) == [])
    check('a vehicle stripped bare keeps it too',
          changed([emptied], sent, set([7169])) == [])
    # The point of the subset test: taking away is refused, replacing is not.
    check('swapping one device for another is a decision, and is recorded',
          [r['tankId'] for r in changed([swapped], sent, set([7169]))] == [7169])
    check('adding a device is recorded',
          [r['tankId'] for r in changed(
              [_with_devices(7169, ['rammer', 'vents', 'stabilizer', 'turbo'])],
              sent, set([7169]))] == [7169])

    # A store written before the devices were kept: nothing to compare against,
    # so nothing is protected and the change goes through rather than sticking.
    old = {7169: {'f': 'whatever', 'd': None}}
    check('a vehicle we know nothing about the devices of is still sent',
          [r['tankId'] for r in changed([stripped], old, set([7169]))] == [7169])


def _with_crew(tank, count, skill='repair'):
    """A record whose crew has this many members."""
    return {'tankId': tank,
            'crew': [{'role': 'commander', 'skills': [skill]} for _ in range(count)]}


class _Tankman(object):
    """Just enough of a tankman for the reader: a role, perks, and whether he is gone."""

    def __init__(self, role, skills, dismissed=False):
        self.role = role
        self.skills = [type('S', (object,), {'name': name})() for name in skills]
        self.bonusSkills = {}
        self.isDismissed = dismissed


class _CrewVehicle(object):
    """A vehicle with some seats filled and a crew that last fought it."""

    def __init__(self, roles, seated, last=None):
        self.crew = list(enumerate(seated))
        self.lastCrew = list(range(len(last or ())))
        self.descriptor = type('D', (object,), {
            'type': type('T', (object,), {'crewRoles': roles})()})()
        self._last = list(last or ())

    def cache(self):
        """The inventory `_returning` looks the last crew up in."""
        holder = self._last
        return type('I', (object,), {
            'getTankman': staticmethod(
                lambda invID: holder[invID] if invID < len(holder) else None)})()


def check_crew_from_last_battle():
    """A crew off driving another vehicle still describes the one it left."""
    from unicum.loadouts import _crew

    roles = (('commander',), ('gunner',), ('driver',))
    away = [_Tankman('commander', ['brotherhood']),
            _Tankman('gunner', ['deadeye']),
            _Tankman('driver', ['smooth_driving'])]

    seated = _CrewVehicle(roles, [_Tankman('commander', ['repair']), None, None], away)
    read = _crew(seated, seated.cache())
    check('whoever is actually sitting there wins over the last crew',
          read[0] == {'role': 'commander', 'skills': ['repair']})
    check('and the empty seats are filled from the crew that last fought it',
          [member['skills'] for member in read[1:]] == [['deadeye'], ['smooth_driving']])

    # The case the player described: the whole crew is in another vehicle.
    empty = _CrewVehicle(roles, [None, None, None], away)
    check('a vehicle whose whole crew left is read as it is played',
          [member['role'] for member in _crew(empty, empty.cache())]
          == ['commander', 'gunner', 'driver'])

    # A crew the player disbanded is genuinely absent, and saying so is the point.
    dismissed = _CrewVehicle(roles, [None, None, None],
                             [_Tankman('commander', ['brotherhood'], dismissed=True)])
    check('a dismissed crew is not brought back', _crew(dismissed, dismissed.cache()) == [])

    # Nothing to return from: the vehicle has never been to battle.
    fresh = _CrewVehicle(roles, [None, None, None])
    check('and a vehicle that never fought has no last crew to read',
          _crew(fresh, fresh.cache()) == [])


def check_crew_away():
    """One crew driving several vehicles must not empty the ones it left."""
    from unicum.loadouts import changed, crewed, fingerprint, uncrewed

    full = _with_crew(7169, 5)
    gone = _with_crew(7169, 0)
    partial = _with_crew(7169, 2)
    retrained = _with_crew(7169, 5, skill='brotherhood')

    check('a record knows how many seats it has somebody in', crewed(full) == 5)
    check('and an absent crew is none of them', crewed(gone) == 0)

    sent = {7169: {'f': fingerprint(full), 'd': None, 'c': crewed(full)}}
    check('a vehicle whose crew went to drive another keeps the one it is played with',
          changed([gone], sent, set([7169])) == [])
    check('so does one the crew only partly left',
          changed([partial], sent, set([7169])) == [])
    # The point of the count: losing members is refused, changing them is not.
    check('a crew taught different perks is a decision, and is recorded',
          [r['tankId'] for r in changed([retrained], sent, set([7169]))] == [7169])
    check('so is a seat that was empty and is now filled',
          [r['tankId'] for r in changed([_with_crew(7169, 6)], sent, set([7169]))] == [7169])
    check('and the rule says nothing about a vehicle we never recorded a crew for',
          not uncrewed(gone, {'f': 'whatever', 'd': None, 'c': None}))


def check_loadout_setting():
    import os
    import tempfile
    from unicum.runtime.session import Session
    from unicum.settings import Settings, validate

    check('loadouts are sent unless the player says otherwise',
          validate({})['sendLoadouts'] is True)
    check('the switch survives a round trip',
          validate({'sendLoadouts': False})['sendLoadouts'] is False)
    settings = Settings(Session(generation=0),
                        store=os.path.join(tempfile.mkdtemp(), 'settings.json'))
    check('a fresh install sends them', settings.sends_loadouts())
    settings.update({'sendLoadouts': False})
    check('turning it off stops them', not settings.sends_loadouts())
    settings.update({'sendLoadouts': True, 'enabled': False})
    check('the mod off sends nothing either', not settings.sends_loadouts())
