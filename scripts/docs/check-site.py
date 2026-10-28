#!/usr/bin/env python3
"""Check files and fragments in built guide HTML. Run on the test server."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
import argparse


class Page(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.links = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if tag == 'a' and attrs.get('name'):
            self.ids.add(attrs['name'])
        key = 'href' if tag == 'a' else 'src' if tag == 'img' else None
        if key and attrs.get(key):
            self.links.append(attrs[key])


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory', nargs='?', default='doc/.vitepress/dist')
parser.add_argument('--base', default='/')
args = parser.parse_args()
root = Path(args.directory).resolve()
base = '/' + args.base.strip('/') + '/' if args.base.strip('/') else '/'
pages = {p: Page(p.read_text()) for p in root.rglob('*.html')}
if not pages:
    raise SystemExit('No built HTML found; build the guide first.')
failures = []
checked = 0
for source, page in pages.items():
    source_url = base + source.relative_to(root).as_posix()
    for href in page.links:
        url = urlsplit(href)
        if (url.hostname == 'github.com' and '/blob/' in url.path
                and unquote(url.path).lower().endswith(('.md', '.markdown'))):
            failures.append(f'{source.relative_to(root)}: documentation link leaves the site: {href}')
            continue
        if url.scheme or url.netloc:
            continue
        resolved = urlsplit(urljoin(source_url, href))
        path = unquote(resolved.path)
        if not path.startswith(base):
            failures.append(f'{source.relative_to(root)}: outside base: {href}')
            continue
        target = root / path[len(base):]
        candidates = [target, target.with_suffix('.html'), target / 'index.html']
        target = next((p for p in candidates if p.is_file()), None)
        checked += 1
        if target is None:
            failures.append(f'{source.relative_to(root)}: missing file: {href}')
        elif resolved.fragment and target in pages:
            fragment = unquote(resolved.fragment)
            if fragment not in pages[target].ids:
                failures.append(f'{source.relative_to(root)}: missing anchor: {href}')
for failure in failures:
    print(failure)
print(f'Checked {len(pages)} HTML pages and {checked} local references; {len(failures)} failures.')
raise SystemExit(bool(failures))
