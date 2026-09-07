"""Persist public media identifiers, never arbitrary credential-bearing URLs."""

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


def canonical_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return None
        host = (parsed.hostname or '').lower().removeprefix('www.')
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        if host in {'youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}:
            allowed = {'v', 'list', 'index', 't', 'si', 'feature', 'ab_channel'}
            if set(query) - allowed:
                return None
            if host == 'youtu.be' and re.fullmatch(r'/[\w-]{6,64}', path):
                return 'https://www.youtube.com/watch?' + urlencode({'v': path[1:]})
            if not re.fullmatch(r'/(?:watch|playlist|(?:shorts|live|channel|c)/[\w-]{1,128}|@[\w.-]{1,128}(?:/(?:videos|streams|shorts))?)', path):
                return None
            query = {key: value[0] for key, value in query.items() if key in {'v', 'list'}
                     and re.fullmatch(r'[\w-]{1,128}', value[0])}
            return urlunparse(('https', 'www.youtube.com', path, '', urlencode(query), ''))
        if host == 'podcasts.apple.com' and re.fullmatch(r'/(?:[a-z]{2}/)?podcast/(?:[\w%-]+/)?id\d+/?', path):
            if set(query) - {'i'} or ('i' in query and not re.fullmatch(r'\d{1,20}', query['i'][0])):
                return None
            return urlunparse(('https', host, path.rstrip('/'), '', urlencode({'i': query['i'][0]}) if query else '', ''))
        if query:
            return None
        patterns = {
            'vimeo.com': r'/\d+', 'player.vimeo.com': r'/video/\d+',
            'instagram.com': r'/(?:p|reel|tv)/[\w-]+/?',
            'tiktok.com': r'/@[\w.-]+/video/\d+',
            'x.com': r'/[\w]+/status/\d+', 'twitter.com': r'/[\w]+/status/\d+',
            'ted.com': r'/talks/[\w-]+', 'dailymotion.com': r'/video/[\w]+',
        }
        pattern = patterns.get(host)
        if host.endswith('.bandcamp.com'):
            pattern = r'/(?:album|track)/[\w-]+'
        if not pattern or not re.fullmatch(pattern, path):
            return None
        return urlunparse(('https', host, path, '', '', ''))
    except (ValueError, TypeError):
        return None
