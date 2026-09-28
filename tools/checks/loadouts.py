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
              old == {7169: {'f': 'oldfingerprint', 'd': None}})
        loadouts.save_sent({7169: {'f': 'abc', 'd': set(['rammer'])}})
        check('what a sweep saves comes back as it went in',
              loadouts.load_sent() == {7169: {'f': 'abc', 'd': set(['rammer'])}})
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
