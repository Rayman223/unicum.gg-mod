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
