"""Validated user options and format selection shared by menus and workers."""

import hashlib
import html
import json
import re

from safe_urls import canonical_url


DEFAULTS = {
    'mode': 'video', 'quality': 'best', 'audio_language': '',
    'subtitle_language': '', 'subtitle_source': 'manual', 'subtitle_format': 'embed',
    'compatible': False, 'sponsorblock': 'default', 'skip_duplicates': True,
}
CHOICES = {
    'mode': ('video', 'mp3', 'opus', 'wav'),
    'quality': ('best', 'auto', '1080', '720', '480', '360'),
    'subtitle_source': ('manual', 'auto'),
    'subtitle_format': ('embed', 'srt', 'vtt', 'txt'),
    'sponsorblock': ('default', 'off', 'mark', 'remove'),
}


def language(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,32}', value))


def validate(values):
    result = dict(DEFAULTS)
    for key, value in values.items():
        if key in CHOICES and value in CHOICES[key]:
            result[key] = value
        elif key in ('audio_language', 'subtitle_language') and (value == '' or language(value)):
            result[key] = value
        elif key in ('compatible', 'skip_duplicates') and isinstance(value, bool):
            result[key] = value
    return result


def playlist_indices(text, maximum, preview_count=200):
    """Only positive indices and ascending closed ranges; never yt-dlp syntax."""
    if len(text) > 300 or not re.fullmatch(r'\s*\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\s*', text):
        raise ValueError('Укажите номера и диапазоны, например: 1,3,7-10.')
    selected = set()
    for part in text.split(','):
        bounds = [int(item.strip()) for item in part.split('-')]
        start, end = bounds[0], bounds[-1]
        if not 1 <= start <= end <= min(200, preview_count):
            raise ValueError('Номера должны входить в показанные первые 200 элементов.')
        selected.update(range(start, end + 1))
        if len(selected) > maximum:
            raise ValueError(f'Можно выбрать не более {maximum} элементов за раз.')
    return ','.join(map(str, sorted(selected)))


def selector(format_id='best', *, audio_only=False, audio_language='', max_height=None, compatible=False):
    if audio_language and not language(audio_language):
        raise ValueError('Некорректный язык аудио')
    if format_id not in ('best', 'bestaudio') and not re.fullmatch(r'[A-Za-z0-9_.-]{1,59}', format_id):
        raise ValueError('Некорректный формат')
    if max_height is not None and (type(max_height) is not int or not 1 <= max_height <= 16384):
        raise ValueError('Некорректное разрешение')
    lang = f'[language={audio_language}]' if audio_language else ''
    audio = f'bestaudio{lang}' + ('[acodec^=mp4a]' if compatible else '')
    height = f'[height<={max_height}]' if max_height else ''
    if audio_only:
        return f'{audio}/best{lang}'
    if compatible:
        return f'bestvideo[vcodec^=avc1]{height}+{audio}/best[vcodec^=avc1][acodec^=mp4a]{height}{lang}'
    if format_id not in ('best', 'bestaudio'):
        # A selected muxed stream must also match the explicitly chosen language.
        return f'{format_id}[acodec=none]+{audio}/{format_id}{lang}/bestvideo{height}+{audio}/best{height}{lang}'
    return f'bestvideo{height}+{audio}/best{height}{lang}'


def auto_format(info, limit, audio_language='', compatible=False):
    """Reserve 8% for muxing/metadata; include the selected audio in every estimate."""
    def size(fmt):
        return fmt.filesize or (int(fmt.tbr * 1000 / 8 * info.duration) if fmt.tbr and info.duration else None)
    audios = [f for f in info.formats if f.is_audio_only
              and (not audio_language or f.language == audio_language)
              and (not compatible or f.acodec.startswith('mp4a'))]
    audio_sizes = [size(f) for f in audios]
    audio_size = max(audio_sizes) if audio_sizes and all(audio_sizes) else None
    candidates = []
    for fmt in info.formats:
        if not fmt.is_video or (compatible and not fmt.vcodec.startswith('avc1')):
            continue
        estimate = size(fmt)
        if fmt.acodec in ('none', '', None):
            estimate = estimate + audio_size if estimate and audio_size else None
        elif (audio_language and fmt.language != audio_language) or (compatible and not fmt.acodec.startswith('mp4a')):
            continue
        if estimate and estimate * 1.08 <= limit:
            height = int(fmt.resolution[:-1]) if re.fullmatch(r'\d+p', fmt.resolution) else 0
            candidates.append((height, fmt.tbr or 0, fmt))
    return max(candidates, key=lambda item: item[:2])[2] if candidates else None


def cache_key(url, options, *, live=False, cookies=False):
    public_url = canonical_url(url)
    if not public_url or live or cookies:
        return None
    payload = json.dumps([public_url, options], sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def subtitle_text(vtt):
    lines, previous, skip = [], None, False
    for line in vtt.splitlines():
        line = line.strip()
        if not line:
            skip = False
            continue
        if line.startswith(('NOTE', 'STYLE', 'REGION')):
            skip = True
        if skip or line.startswith(('WEBVTT', 'Kind:', 'Language:')) or '-->' in line or line.isdigit():
            continue
        line = html.unescape(re.sub(r'<[^>]*>', '', line))
        if line and line != previous:
            lines.append(line)
            previous = line
    return '\n'.join(lines) + '\n'
