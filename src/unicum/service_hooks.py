"""What the client's battle results service offers to sit on.

Where to sit has now been a guess twice, and both guesses looked right from
outside. `onResultPosted` existed, reported itself as attached, and never
fired. `postResult` fired once and then not again, which is what a method that
runs when the player opens the results screen looks like -- and the screen is
exactly what this module must not depend on, because a player who queues
straight into the next battle never opens it.

So the surface is read off the service and logged once, names only. Nothing
here calls anything: a name is evidence, and calling a client's methods to find
out what they do would be this mod running code it knows nothing about.
"""
import logging

_logger = logging.getLogger('unicum.service_hooks')

# How many names a line carries before it stops being readable.
_MAX_NAMES = 60

# What the types below are not, however much they answer to `+=`.
_NOT_AN_EVENT = (basestring, bool, int, long, float, list, tuple, dict, set)


def is_event(value):
    """Whether this looks like a WG Event: something to attach to and detach from.

    Both halves are required. A list answers to `+=` and would be subscribed
    to happily, then never fire anything.
    """
    if isinstance(value, _NOT_AN_EVENT):
        return False
    return hasattr(value, '__iadd__') and hasattr(value, '__isub__')


def surface(service):
    """(the events, the methods) a service offers, as sorted name lists."""
    events, methods = [], []
    for name in dir(service):
        if name.startswith('__'):
            continue
        try:
            attr = getattr(service, name)
        except Exception:
            # A property that raises is the client's business, not ours.
            continue
        if is_event(attr):
            events.append(name)
        elif callable(attr):
            methods.append(name)
    return sorted(events), sorted(methods)


def account_events():
    """The client's account-level events, or None if they cannot be reached.

    Looked at because the results service turned out to speak only when the
    player looks: both of its hooks fire as the results screen is built, and
    the screen is the one thing the capture must not depend on. An arrival is
    announced somewhere that does not care whether anyone is watching, and
    this is where a client announces things to the account.
    """
    try:
        from PlayerEvents import g_playerEvents
        return g_playerEvents
    except Exception:
        _logger.debug('no account events in this client', exc_info=True)
        return None


def about_battles(names):
    """The names worth reading twice, out of a surface that holds hundreds."""
    return [name for name in names
            if 'battle' in name.lower() or 'result' in name.lower()]


def describe(service):
    """Log what there is to sit on, once, so the next choice is read not guessed."""
    try:
        events, methods = surface(service)
        _logger.info('%s offers events [%s] and methods [%s]',
                     type(service).__name__,
                     ', '.join(events[:_MAX_NAMES]) or 'none',
                     ', '.join(methods[:_MAX_NAMES]) or 'none')
    except Exception:
        _logger.warning('the results service could not be described')
    try:
        account = account_events()
        if account is None:
            return
        events, methods = surface(account)
        # Filtered, unlike the service: this surface holds hundreds of names
        # and only the ones naming a battle or a result can be the arrival.
        _logger.info('the account announces battles on events [%s] and methods [%s]',
                     ', '.join(about_battles(events)[:_MAX_NAMES]) or 'none',
                     ', '.join(about_battles(methods)[:_MAX_NAMES]) or 'none')
    except Exception:
        _logger.warning('the account events could not be described')
