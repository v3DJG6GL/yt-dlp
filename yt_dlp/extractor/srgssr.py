from .common import InfoExtractor
from ..utils import (
    ExtractorError,
    float_or_none,
    int_or_none,
    join_nonempty,
    parse_iso8601,
    qualities,
    try_get,
)


class SRGSSRIE(InfoExtractor):
    _VALID_URL = r'''(?x)
                    (?:
                        https?://tp\.srgssr\.ch/p(?:/[^/]+)+\?urn=urn|
                        srgssr
                    ):
                    (?P<bu>
                        srf|rts|rsi|rtr|swi
                    ):(?:[^:]+:)?
                    (?P<type>
                        video|audio
                    ):
                    (?P<id>
                        [0-9a-f\-]{36}|\d+
                    )
                    '''
    _GEO_BYPASS = False
    _GEO_COUNTRIES = ['CH']

    _ERRORS = {
        'AGERATING12': 'To protect children under the age of 12, this video is only available between 8 p.m. and 6 a.m.',
        'AGERATING18': 'To protect children under the age of 18, this video is only available between 11 p.m. and 5 a.m.',
        # 'ENDDATE': 'For legal reasons, this video was only available for a specified period of time.',
        'GEOBLOCK': 'For legal reasons, this video is only available in Switzerland.',
        'LEGAL': 'The video cannot be transmitted for legal reasons.',
        'STARTDATE': 'This video is not yet available. Please try again later.',
    }
    _DEFAULT_LANGUAGE_CODES = {
        'srf': 'de',
        'rts': 'fr',
        'rsi': 'it',
        'rtr': 'rm',
        'swi': 'en',
    }

    def _get_tokenized_src(self, url, video_id, format_id):
        token = self._download_json(
            'http://tp.srgssr.ch/akahd/token?acl=*',
            video_id, f'Downloading {format_id} token', fatal=False) or {}
        auth_params = try_get(token, lambda x: x['token']['authparams'])
        if auth_params:
            url += ('?' if '?' not in url else '&') + auth_params
        return url

    def _get_media_data(self, bu, media_type, media_id):
        query = {'onlyChapters': True} if media_type == 'video' else {}
        full_media_data = self._download_json(
            f'https://il.srgssr.ch/integrationlayer/2.0/{bu}/mediaComposition/{media_type}/{media_id}.json',
            media_id, query=query)

        chapter_list = full_media_data.get('chapterList', [])
        if not chapter_list:
            raise ExtractorError('No chapters found')

        try:
            media_data = next(
                ch for ch in chapter_list
                if ch.get('id') == media_id or ch.get('urn') == f'urn:{bu}:video:{media_id}'
            )
        except StopIteration:
            media_data = chapter_list[0]

        block_reason = media_data.get('blockReason')
        if block_reason and block_reason in self._ERRORS:
            message = self._ERRORS[block_reason]
            if block_reason == 'GEOBLOCK':
                self.raise_geo_restricted(
                    msg=message, countries=self._GEO_COUNTRIES)
            raise ExtractorError(f'{self.IE_NAME} said: {message}', expected=True)

        media_data['parentChapter'] = next(
            (ch for ch in chapter_list if not ch.get('fullLengthUrn')),
            chapter_list[0]
        )
        return media_data, full_media_data

    def _extract_m3u8_subtitles(self, m3u8_url, media_id):
        """Extract subtitles directly from an m3u8 playlist."""
        from urllib.parse import urljoin
        import re

        m3u8_content = self._download_webpage(m3u8_url, media_id, 'Downloading m3u8 playlist', fatal=False)
        if not m3u8_content:
            return {}

        subtitles = {}

        # Look for #EXT-X-MEDIA:TYPE=SUBTITLES lines
        sub_entries = re.findall(r'#EXT-X-MEDIA:TYPE=SUBTITLES,(.*)', m3u8_content)
        for entry in sub_entries:
            # Parse the attributes of the subtitle entry
            attrs = {}
            for attr_match in re.finditer(r'([A-Z-]+)="([^"]*)"', entry):
                attrs[attr_match.group(1)] = attr_match.group(2)

            if 'URI' not in attrs or 'LANGUAGE' not in attrs:
                continue

            sub_url = attrs['URI']
            # Make the URL absolute if it's not already
            if not sub_url.startswith('http'):
                sub_url = urljoin(m3u8_url, sub_url)

            lang = attrs['LANGUAGE']
            name = attrs.get('NAME', '')

            subtitles.setdefault(lang, []).append({
                'url': sub_url,
                'name': name,
            })

        return subtitles

    def _real_extract(self, url):
        bu, media_type, media_id = self._match_valid_url(url).groups()
        media_data, full_media_data = self._get_media_data(bu, media_type, media_id)

        is_segment = bool(media_data.get('fullLengthUrn'))
        mark_in = int_or_none(media_data.get('markIn'))
        mark_out = int_or_none(media_data.get('markOut'))

        formats = []
        subtitles = {}
        q = qualities(['SD', 'HD'])

        resource_list = media_data.get('resourceList') or []
        if not resource_list and is_segment:
            resource_list = media_data.get('parentChapter', {}).get('resourceList', [])

        for source in resource_list:
            format_url = source.get('url')
            if not format_url:
                continue

            protocol = source.get('protocol')
            quality = source.get('quality')
            format_id = join_nonempty(protocol, source.get('encoding'), quality)

            if is_segment and mark_in is not None and mark_out is not None:
                start = mark_in / 1000
                end = mark_out / 1000
                if protocol == 'HLS' and '?' in format_url:
                    base, query = format_url.split('?', 1)
                    params = {}
                    for param in query.split('&'):
                        k, _, v = param.partition('=')
                        params[k] = v
                    params.update({'start': f'{start:.3f}', 'end': f'{end:.3f}'})
                    format_url = f'{base}?{"&".join(f"{k}={v}" for k, v in params.items())}'

            if protocol in ('HDS', 'HLS'):
                if source.get('tokenType') == 'AKAMAI':
                    format_url = self._get_tokenized_src(format_url, media_id, format_id)
                    fmts, subs = self._extract_akamai_formats_and_subtitles(format_url, media_id)
                    formats.extend(fmts)
                    self._merge_subtitles(subtitles, subs)
                elif protocol == 'HLS':
                    m3u8_fmts, m3u8_subs = self._extract_m3u8_formats_and_subtitles(
                        format_url, media_id, 'mp4', 'm3u8_native', m3u8_id=format_id, fatal=False)
                    formats.extend(m3u8_fmts)
                    self._merge_subtitles(subtitles, m3u8_subs)

                    # Also try to extract subtitles directly from the m3u8 playlist
                    direct_m3u8_subs = self._extract_m3u8_subtitles(format_url, media_id)
                    subtitles = self._merge_subtitles(subtitles, direct_m3u8_subs)
            elif protocol in ('HTTP', 'HTTPS'):
                formats.append({
                    'format_id': format_id,
                    'url': format_url,
                    'quality': q(quality),
                })

        # This is needed because for audio medias the podcast url is usually
        # always included, even if is only an audio segment and not the
        # whole episode.
        if int_or_none(media_data.get('position')) == 0:
            for p in ('S', 'H'):
                podcast_url = media_data.get(f'podcast{p}dUrl')
                if not podcast_url:
                    continue
                quality = p + 'D'
                formats.append({
                    'format_id': 'PODCAST-' + quality,
                    'url': podcast_url,
                    'quality': q(quality),
                })

        if media_type == 'video':
            for sub in (media_data.get('subtitleList') or []):
                sub_url = sub.get('url')
                if not sub_url:
                    continue
                lang = sub.get('locale') or self._DEFAULT_LANGUAGE_CODES[bu]
                subtitles.setdefault(lang, []).append({
                    'url': sub_url,
                })

        return {
            'id': media_id,
            'title': media_data.get('title'),
            'description': media_data.get('description') or media_data.get('lead'),
            'timestamp': parse_iso8601(media_data.get('date')),
            'thumbnail': media_data.get('imageUrl'),
            'duration': float_or_none(media_data.get('duration'), 1000),
            'subtitles': subtitles,
            'formats': formats,
            'series': full_media_data.get('show', {}).get('title') or media_data.get('title'),
            'season_number': int_or_none(full_media_data.get('episode', {}).get('seasonNumber')),
            'episode_number': int_or_none(full_media_data.get('episode', {}).get('number')),
            'channel': media_data.get('vendor'),
        }

class SRGSSRPlayIE(InfoExtractor):
    IE_DESC = 'srf.ch, rts.ch, rsi.ch, rtr.ch and swissinfo.ch play sites'
    _VALID_URL = r'''(?x)
                    https?://
                        (?:(?:www|play)\.)?
                        (?P<bu>srf|rts|rsi|rtr|swissinfo)\.ch/play/(?:tv|radio)/
                        (?:
                            [^/]+/(?P<type>video|audio)/[^?]+|
                            popup(?P<type_2>video|audio)player
                        )
                        \?.*?\b(?:id=|urn=urn:[^:]+:video:)(?P<id>[0-9a-f\-]{36}|\d+)
                    '''
    _TESTS = [{
        'url': 'http://www.srf.ch/play/tv/10vor10/video/snowden-beantragt-asyl-in-russland?id=28e1a57d-5b76-4399-8ab3-9097f071e6c5',
        'md5': '6db2226ba97f62ad42ce09783680046c',
        'info_dict': {
            'id': '28e1a57d-5b76-4399-8ab3-9097f071e6c5',
            'ext': 'mp4',
            'upload_date': '20130701',
            'title': 'Snowden beantragt Asyl in Russland',
            'timestamp': 1372708215,
            'duration': 113.827,
            'thumbnail': r're:^https?://.*1383719781\.png$',
        },
        'expected_warnings': ['Unable to download f4m manifest'],
    }, {
        'url': 'http://www.rtr.ch/play/radio/actualitad/audio/saira-tujetsch-tuttina-cuntinuar-cun-sedrun-muster-turissem?id=63cb0778-27f8-49af-9284-8c7a8c6d15fc',
        'info_dict': {
            'id': '63cb0778-27f8-49af-9284-8c7a8c6d15fc',
            'ext': 'mp3',
            'upload_date': '20151013',
            'title': 'Saira: Tujetsch - tuttina cuntinuar cun Sedrun Mustér Turissem',
            'timestamp': 1444709160,
            'duration': 336.816,
        },
        'params': {
            # rtmp download
            'skip_download': True,
        },
    }, {
        'url': 'http://www.rts.ch/play/tv/-/video/le-19h30?id=6348260',
        'md5': '67a2a9ae4e8e62a68d0e9820cc9782df',
        'info_dict': {
            'id': '6348260',
            'display_id': '6348260',
            'ext': 'mp4',
            'duration': 1796.76,
            'title': 'Le 19h30',
            'upload_date': '20141201',
            'timestamp': 1417458600,
            'thumbnail': r're:^https?://.*\.image',
        },
        'params': {
            # m3u8 download
            'skip_download': True,
        },
    }, {
        'url': 'http://play.swissinfo.ch/play/tv/business/video/why-people-were-against-tax-reforms?id=42960270',
        'info_dict': {
            'id': '42960270',
            'ext': 'mp4',
            'title': 'Why people were against tax reforms',
            'description': 'md5:7ac442c558e9630e947427469c4b824d',
            'duration': 94.0,
            'upload_date': '20170215',
            'timestamp': 1487173560,
            'thumbnail': r're:https?://www\.swissinfo\.ch/srgscalableimage/42961964',
            'subtitles': 'count:9',
        },
        'params': {
            'skip_download': True,
        },
    }, {
        'url': 'https://www.srf.ch/play/tv/popupvideoplayer?id=c4dba0ca-e75b-43b2-a34f-f708a4932e01',
        'only_matching': True,
    }, {
        'url': 'https://www.srf.ch/play/tv/10vor10/video/snowden-beantragt-asyl-in-russland?urn=urn:srf:video:28e1a57d-5b76-4399-8ab3-9097f071e6c5',
        'only_matching': True,
    }, {
        'url': 'https://www.rts.ch/play/tv/19h30/video/le-19h30?urn=urn:rts:video:6348260',
        'only_matching': True,
    }, {
        # audio segment, has podcastSdUrl of the full episode
        'url': 'https://www.srf.ch/play/radio/popupaudioplayer?id=50b20dc8-f05b-4972-bf03-e438ff2833eb',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        bu = mobj.group('bu')
        media_type = mobj.group('type') or mobj.group('type_2')
        media_id = mobj.group('id')
        return self.url_result(f'srgssr:{bu[:3]}:{media_type}:{media_id}', 'SRGSSR')
