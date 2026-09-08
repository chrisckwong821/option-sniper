"""Prevent new dashboard code from reusing stale browser/CDN asset URLs."""
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1] / 'dashboard'


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and 'src' in attrs:
            self.urls.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.urls.append(attrs['href'])


class AssetVersionTests(unittest.TestCase):
    def test_every_dashboard_asset_version_matches_its_content(self):
        parser = AssetParser()
        parser.feed((ROOT / 'index.html').read_text())
        self.assertGreaterEqual(len(parser.urls), 4)
        for url in parser.urls:
            with self.subTest(url=url):
                parts = urlsplit(url)
                self.assertFalse(parts.netloc, 'Review external assets explicitly')
                expected = hashlib.sha256((ROOT / parts.path).read_bytes()).hexdigest()[:16]
                self.assertEqual(parse_qs(parts.query).get('v'), [expected],
                                 'Update the asset URL hash when its content changes')
