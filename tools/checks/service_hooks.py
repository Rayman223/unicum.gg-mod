"""Checks for reading what a results service offers to sit on.

The point of this surface is that it is read rather than guessed, so what is
checked is that it tells an event from everything else that merely answers to
`+=`, that it never calls what it finds, and that a client property which
raises does not take the description down with it.
"""
import logging

from checks.common import check


class _Event(object):
    def __iadd__(self, handler):
        return self

    def __isub__(self, handler):
        return self


class _Capture(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def said(self, fragment):
        return any(fragment in line for line in self.lines)


def check_service_hooks():
    from unicum.service_hooks import describe, is_event, surface

    called = []

    class _Service(object):
        onResultPosted = _Event()

        def __init__(self):
            self.counters = [1, 2]
            self.label = 'results'
            self.total = 3

        def postResult(self, result):
            called.append(result)

        @property
        def angry(self):
            raise RuntimeError('a client property that raises')

    # A list answers to `+=` and would be subscribed to happily, then never
    # fire anything. Both halves of the protocol are required.
    check('an event is something to attach to and detach from', is_event(_Event()))
    check('a list is not an event', not is_event([1, 2]))
    check('a string is not an event', not is_event('results'))
    check('a number is not an event', not is_event(3))

    events, methods = surface(_Service())
    check('the events are found', events == ['onResultPosted'])
    check('the methods are found', 'postResult' in methods)
    check('a plain value is neither', 'label' not in methods and 'label' not in events)
    # Reading a surface must not set anything off: the mod would be running
    # client code it knows nothing about, at the end of every battle.
    check('nothing found was called', not called)

    captured = _Capture()
    logger = logging.getLogger('unicum.service_hooks')
    logger.addHandler(captured)
    try:
        describe(_Service())
    finally:
        logger.removeHandler(captured)

    check('the description names the service', captured.said('_Service'))
    check('and what it offers', captured.said('onResultPosted') and captured.said('postResult'))
    check('a property that raises does not take the description down',
          not captured.said('angry'))
