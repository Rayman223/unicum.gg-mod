"""Checks for the meter that says what the mod costs the game thread."""

from checks.common import check


def check_perf_meter():
    from unicum.runtime import perf

    was_on = perf.enabled()
    try:
        perf.stop()
        perf.clear()

        calls = []
        wrapped = perf.track('somewhere.slow', lambda x: calls.append(x) or x * 2)

        check('a wrapped call still returns what it returned', wrapped(21) == 42)
        check('and still gets its arguments', calls == [21])
        # Off by default, and the point of that is not tidiness: a meter that
        # measured whether asked or not would be a cost every player paid for
        # a question almost none of them have.
        check('nothing is measured until somebody asks', perf.rows() == [])

        perf.start()
        check('starting turns it on', perf.enabled())
        for _ in range(5):
            wrapped(1)
        rows = perf.rows()
        check('a measured call is counted under its own name',
              len(rows) == 1 and rows[0][0] == 'somewhere.slow' and rows[0][1] == 5)
        check('with a total, a worst case and a share of the clock',
              rows[0][2] >= 0.0 and rows[0][3] >= 0.0 and rows[0][4] >= 0.0)

        # The report covers the interval since the last one: a garage opening
        # must not go on dominating the table ten minutes later.
        perf.clear()
        check('clearing forgets what was measured', perf.rows() == [])

        perf.start()
        quick = perf.track('somewhere.quick', lambda: None)
        for _ in range(3):
            quick()
        for _ in range(30):
            wrapped(1)
        names = [row[0] for row in perf.rows()]
        check('the table is worst first', names[0] == 'somewhere.slow')
        check('and names everything measured', sorted(names) == ['somewhere.quick', 'somewhere.slow'])

        perf.stop()
        before = perf.rows()[0][1]
        wrapped(1)
        check('stopping stops counting', perf.rows()[0][1] == before)

        # A hook that must stay reachable: restoring a patch and detaching an
        # event both need the thing that was wrapped, not the wrapper.
        check('the wrapper keeps hold of what it wraps',
              perf.track('x', check).unmeasured is check)
    finally:
        perf.stop()
        perf.clear()
        if was_on:
            perf.start()


def check_perf_naming():
    """A table is only useful if each line names somewhere to go and look."""
    from unicum.runtime.session import _where

    class Thing(object):
        def method(self):
            pass

    def plain():
        pass

    check('a bound method is named by its class and method',
          _where(Thing().method).endswith('Thing.method'))
    check('a plain function is named by its module',
          _where(plain).endswith('.plain'))
    # Every name in this mod would otherwise start with the same nine
    # characters, which is nine characters of a narrow table saying nothing.
    check('and the package prefix is dropped',
          not _where(Thing().method).startswith('unicum.'))


def check_perf_setting():
    import os
    import tempfile
    from unicum.runtime.session import Session
    from unicum.settings import Settings, validate

    check('measuring is off until asked for', validate({})['measurePerformance'] is False)
    check('and survives a round trip', validate({'measurePerformance': True})['measurePerformance'] is True)
    settings = Settings(Session(generation=0),
                        store=os.path.join(tempfile.mkdtemp(), 'settings.json'))
    check('a fresh install measures nothing', not settings.measures_performance())
    settings.update({'measurePerformance': True})
    check('ticking the box measures', settings.measures_performance())
    # Not gated on `enabled`: turning the mod off to compare frame rates is
    # exactly when the measurement is wanted, and a meter that went quiet
    # then would answer every question with silence.
    settings.update({'enabled': False})
    check('and keeps measuring with the mod otherwise off', settings.measures_performance())


def check_sampler_owner():
    """A table is only an answer if each line names something a player installed."""
    from unicum.runtime.sampler import _owner

    check('code from a .wotmod is charged to that archive',
          _owner('C:/Games/World_of_Tanks_EU/mods/2.4.0.1/aslain.modmenu_2.0.06.wotmod/'
                 'gui/mods/mod_menu.py') == 'aslain.modmenu_2.0.06.wotmod')
    check('whatever the slashes and the case',
          _owner(r'c:\Games\mods\2.4.0.1\Izeberg.ModsSettingsApi_1.7.0.wotmod\x.py')
          == 'izeberg.modssettingsapi_1.7.0.wotmod')
    check('a loose res_mods script is charged to the script',
          _owner('C:/Games/World_of_Tanks_EU/res_mods/2.4.0.1/scripts/client/gui/mods/'
                 'mod_something.py') == 'res_mods/mod_something.py')
    # However this package was loaded: the dev bootstrap reports a working
    # copy, a release build a path inside its own archive. Both are us, and
    # a report that hid our own cost would be the one thing it must not do.
    check('this mod is named as itself, loaded from a working copy',
          _owner('D:/workspace/wot/unicum.gg-mod/src/unicum/battle.py')
          == 'unicum.gg (this mod)')
    check('and the client is what is left',
          _owner('scripts/client/gui/Scaleform/daapi/view/battle/shared/page.py') == 'the client'
          and _owner(None) == 'the client')


def check_sampler_lifecycle():
    """It runs on a thread, so a reload that left one behind would be a leak."""
    import time
    from unicum.runtime import sampler

    check('nothing samples until asked', not sampler.running())
    sampler.start()
    try:
        check('starting starts it', sampler.running())
        # Enough wall clock for the thread to take samples of this very test.
        time.sleep(0.15)
        owners, places, samples, idle, elapsed = sampler.rows()
        check('and it takes samples of the main thread', samples > 0)
        check('every sample is either in the engine or in somebody else code',
              idle + sum(count for _, count in owners) == samples)
        check('the places add up the same way',
              sum(count for _, count in places) == samples - idle)
    finally:
        sampler.stop()
    check('stopping stops it', not sampler.running())
    sampler.clear()
    check('and clearing forgets what it saw', sampler.rows()[2] == 0)
