import hashlib
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen

SOURCE = 'https://trends.google.com/trending/rss?geo=KR'
MAX_BYTES = 5 * 1024 * 1024

def fetch(timeout=20):
    with urlopen(Request(SOURCE, headers={'User-Agent': 'FoodTrendsLocalMVP/1.0', 'Accept': 'application/rss+xml, text/xml'}), timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('RSS 응답이 5MB 제한을 초과했습니다')
        return data, response.status

def local(tag):
    return tag.rsplit('}', 1)[-1]

def fields(node):
    return {local(child.tag): (child.text or '').strip() for child in node}

def parse(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('DTD/ENTITY가 포함된 RSS는 지원하지 않습니다')
    root = ET.fromstring(data)
    channel = root.find('channel')
    if local(root.tag) != 'rss' or channel is None:
        raise ValueError('지원하는 RSS/channel 형식이 아닙니다')
    items, warnings = [], []
    for node in channel.findall('item'):
        f = fields(node)
        if not f.get('title'):
            raise ValueError('검색어 title이 없는 RSS 항목: 응답 형식을 확인하세요')
        published = None
        if f.get('pubDate'):
            try:
                dt = parsedate_to_datetime(f['pubDate'])
                if dt.tzinfo is None:
                    raise ValueError('timezone missing')
                published = dt.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                warnings.append('pubDate 해석 실패: ' + f['pubDate'])
        news = []
        related = []
        for child in node:
            tag = local(child.tag)
            if tag == 'news_item':
                nf = fields(child)
                news.append({'title': nf.get('news_item_title'), 'url': nf.get('news_item_url'),
                             'source': nf.get('news_item_source'), 'snippet': nf.get('news_item_snippet')})
            if tag in ('related_query', 'related_search', 'related_queries', 'related_searches'):
                related.extend(t.strip() for t in child.itertext() if t.strip())
        raw = ET.tostring(node, encoding='unicode')
        # Missing publication date: stable title/link identity; never invent a date.
        identity = '\x1f'.join([SOURCE, f['title'], published or f.get('pubDate', ''), f.get('link', '')])
        items.append({'source_id': hashlib.sha256(identity.encode()).hexdigest(), 'keyword': f['title'],
                      'published_at': published, 'published_raw': f.get('pubDate'),
                      'traffic': f.get('approx_traffic'), 'news': news,
                      'related_queries': related if related else None, 'raw_xml': raw,
                      'provided_fields': sorted(set(local(c.tag) for c in node))})
    if not items:
        warnings.append('RSS 항목이 없습니다')
    return items, warnings
