"""网站识别：离线服务归属，以及 Agent 返回的网页元信息。"""

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from backend.utils.region_check import SERVICES

# Go RE2 支持的表达式；每条捕获最多 200 字节，由 Agent 限制。
PAGE_MARKERS = {
    'title': r'(?is)<title\b[^>]*>(.*?)</title>',
    'site': r'''(?is)<meta\b[^>]*(?:property|name)\s*=\s*["']og:site_name["'][^>]*>''',
    'description': r'''(?is)<meta\b[^>]*(?:name|property)\s*=\s*["'](?:description|og:description)["'][^>]*>''',
    'challenge': r'(?i)cf-chl-|challenge-platform|just a moment|checking your browser|verify you are human|人机验证|安全验证',
}


# 标题里常见的页面名 / 程序署名，如 NexusPHP 的「站名 :: 登录 - Powered by NexusPHP」
_TITLE_SEPARATOR = r'(?:\s*::\s*|\s+[|\-–—·]\s+)'
_TITLE_NOISE = r'(?:用户登录|会员登录|登录|登入|注册|首页|主页|log\s*in|sign\s*in|home|powered\s+by\s+[^|:]+)'


def site_title(title):
    cleaned, previous = title, None
    while cleaned != previous:
        previous = cleaned
        cleaned = re.sub(rf'(?i){_TITLE_SEPARATOR}{_TITLE_NOISE}\s*$', '', cleaned)
        cleaned = re.sub(rf'(?i)^{_TITLE_NOISE}{_TITLE_SEPARATOR}', '', cleaned)
    return cleaned.strip() or title


def clean_text(value, limit=200):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]*>', '', value or ''))).strip()[:limit]


class _Meta(HTMLParser):
    content = ''

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'meta':
            self.content = dict(attrs).get('content') or ''


def _meta_content(value):
    parser = _Meta()
    parser.feed(value or '')
    return clean_text(parser.content)


def known_service(host):
    for service in SERVICES:
        for suffix in service['domains']:
            if host == suffix or host.endswith('.' + suffix):
                return {'name': 'YouTube' if service['id'] == 'youtube' else service['name'], 'source': 'catalog',
                        'category': 'AI 服务' if service['id'] in ('openai', 'claude') else '视频服务',
                        'evidence': f'内置服务域名：{suffix}'}
    return None


def page_identity(result, host, checked_at):
    """网页内容是自报信息，不当作经过验证的服务归属。"""
    result = result or {}
    markers = result.get('markers') or {}
    identity = {'source': 'page', 'host': host, 'checked_at': checked_at,
                'name': '', 'description': '', 'reason': '', 'status': result.get('status')}
    if result.get('error'):
        identity['reason'] = f"访问失败：{result['error']}"
        return identity
    if not 200 <= (result.get('status') or 0) < 300:
        identity['reason'] = f"页面未成功返回（HTTP {result.get('status') or '未知'}）"
        return identity
    if markers.get('challenge'):
        identity['reason'] = '页面要求人机验证，无法识别'
        return identity
    title = clean_text(markers.get('title'))
    if re.search(r'(?i)^(?:access denied|forbidden|not found|error|404|403|502|503)\b', title):
        identity['reason'] = '返回错误页，无法识别'
        return identity
    identity['name'] = _meta_content(markers.get('site')) or site_title(title)
    identity['description'] = _meta_content(markers.get('description'))
    # 不保留 URL 路径和查询参数，避免把重定向中的令牌写进缓存。
    try:
        identity['final_host'] = urlsplit(result.get('final_url') or '').hostname or host
    except ValueError:
        identity['final_host'] = host
    if not identity['name']:
        identity['reason'] = '未找到网站名称，可能是 API 或资源域名'
    return identity
