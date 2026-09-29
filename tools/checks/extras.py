# -*- coding: utf-8 -*-
"""Checks for the win rate and the battle count drawn beside a rating."""

from checks.common import check


def check_battle_count():
    """A number a player reads at a glance, in thirty-six languages."""
    from unicum.badges import count

    check('a small count is written out', count(72) == u'72')
    check('and grouped once it is long', count(22565) == u'22 565')
    check('grouped again past a million', count(1234567) == u'1 234 567')
    check('the boundary is not grouped', count(999) == u'999')
    check('and just past it is', count(1000) == u'1 000')
    # A thin space, not a comma and not a dot: both are decimal separators
    # somewhere this mod is played, and "22,565" reading as twenty-two is the
    # kind of wrong nobody notices.
    check('nothing that could be read as a decimal point',
          ',' not in count(22565) and '.' not in count(22565))

    check('the players panel shortens what will not fit', count(22565, compact=True) == '22.6k')
    check('but only past the point where the digits stop mattering',
          count(9999, compact=True) == u'9 999' and count(10000, compact=True) == '10.0k')
    check('the short form rounds rather than truncates',
          count(22565, compact=True) == '22.6k' and count(22549, compact=True) == '22.5k')
    check('zero is a real answer and is shown', count(0) == u'0')


class _Entry(object):
    """An entry as the lookup hands it over."""

    def __init__(self, ratings, known=True):
        self.known = known
        self.ratings = ratings

    def stat(self, name, window):
        value = (self.ratings.get(window) or {}).get(name)
        return value if isinstance(value, (int, float)) else None


class _Settings(object):
    """Only what `extras` asks of the settings."""

    def __init__(self, winrate=None, battles=None, rated=True):
        self._winrate, self._battles, self._rated = winrate, battles, rated

    def metric(self, surface):
        return 'wnx' if self._rated else None

    def extra(self, name, surface):
        if not self._rated:
            return None
        return self._winrate if name == 'winrate' else self._battles


def check_extras_rendering():
    from unicum.badges import Badges

    # An empty directory means the badges cannot draw, which is what we want:
    # `extras` is about the text beside a badge, not the badge.
    badges = Badges(scales=None, directory='', drawable=lambda path: False)
    entry = _Entry({'total': {'battles': 22565, 'winrate': 59.583425},
                    'recent': {'battles': 72, 'winrate': 52.777}})

    out = badges.extras(entry, _Settings(winrate='total', battles='total'), 'battle')
    check('both figures are drawn, win rate first',
          u'59.6%' in out and u'22 565' in out and out.index('59.6') < out.index('22'))
    check('and they are dimmed, so the badge stays the thing you read',
          'color="#8a8a8a"' in out)
    check('with a leading space, since they follow a badge', out.startswith(' '))

    check('the players panel gets the short form',
          '22.6k' in badges.extras(entry, _Settings(battles='total'), 'battle', compact=True))

    # Each window is its own: a career total shown where 30 days were asked
    # for would be wrong by three orders of magnitude, unnoticeably.
    out = badges.extras(entry, _Settings(winrate='recent', battles='recent'), 'battle')
    check('the 30-day window shows the 30-day figures', '52.8%' in out and '72' in out)

    check('either can be off on its own',
          badges.extras(entry, _Settings(battles='total'), 'battle').strip().startswith('<font')
          and '%' not in badges.extras(entry, _Settings(battles='total'), 'battle'))
    check('both off draws nothing at all',
          badges.extras(entry, _Settings(), 'battle') == '')
    # Tied to the rating: these are read beside it, so a surface that shows
    # no rating shows nothing beside one.
    check('a surface showing no rating shows none of this',
          badges.extras(entry, _Settings(winrate='total', battles='total', rated=False), 'battle') == '')

    check('a player the server knows nothing about draws nothing',
          badges.extras(_Entry({}, known=False), _Settings(battles='total'), 'battle') == '')
    check('nor does one whose window is empty',
          badges.extras(_Entry({'total': {}}), _Settings(winrate='total', battles='total'), 'battle') == '')


def check_extras_setting():
    import os
    import tempfile
    from unicum.runtime.session import Session
    from unicum.settings import Settings, validate

    check('battles are shown over a whole career by default', validate({})['battles'] == 'total')
    check('so is the win rate', validate({})['winrate'] == 'total')
    check('a nonsense value falls back to the default',
          validate({'battles': 'yesterday'})['battles'] == 'total')
    check('and a real one survives', validate({'battles': 'recent'})['battles'] == 'recent')

    settings = Settings(Session(generation=0),
                        store=os.path.join(tempfile.mkdtemp(), 'settings.json'))
    check('a fresh install draws both over the career',
          settings.extra('battles', 'battle') == 'total'
          and settings.extra('winrate', 'battle') == 'total')
    settings.update({'battles': 'off'})
    check('off means nothing to draw', settings.extra('battles', 'battle') is None)
    check('and leaves the win rate alone', settings.extra('winrate', 'battle') == 'total')
    settings.update({'battle': {'rating': False}})
    check('a surface with its rating turned off draws neither',
          settings.extra('winrate', 'battle') is None)
    settings.update({'enabled': False})
    check('the mod off draws neither anywhere',
          settings.extra('winrate', 'contacts') is None)


def check_entry_stat():
    """Unlike a rating, these never fall back to the other window."""
    from unicum.api.entry import Entry

    entry = Entry(known=True, ratings={'total': {'battles': 22565, 'winrate': 59.5},
                                       'recent': {'battles': None, 'winrate': None}})
    check('a figure comes from the window it was asked for',
          entry.stat('battles', 'total') == 22565)
    check('and a window the server has not filled says so, rather than lying',
          entry.stat('battles', 'recent') is None)
    check('a window that does not exist at all is the same answer',
          entry.stat('battles', 'nonsense') is None)
    # The rating DOES fall back, deliberately, and that difference is the
    # whole point of having two accessors.
    check('while a rating still falls back to the career value',
          Entry(known=True, ratings={'total': {'wnx': 3274}, 'recent': {'wnx': None}})
          .rating('wnx', 'recent') == 3274)
