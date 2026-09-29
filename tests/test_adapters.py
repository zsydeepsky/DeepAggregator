from app.core.sources.arxiv import parse_arxiv_atom
from app.core.sources.rss import parse_feed

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <updated>2024-01-01T00:00:00Z</updated>
    <published>2024-01-01T00:00:00Z</published>
    <title>  A Study of
    Embeddings </title>
    <summary>  We study embeddings. </summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
  </entry>
</feed>
"""

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>News &amp; Views</title>
    <link>https://example.com/a</link>
    <guid>guid-1</guid>
    <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
    <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""


def test_parse_arxiv():
    items = parse_arxiv_atom(ARXIV_XML)
    assert len(items) == 1
    item = items[0]
    assert item.guid.startswith("http://arxiv.org/abs/")
    assert item.title == "A Study of Embeddings"
    assert item.summary == "We study embeddings."
    assert item.author == "Alice, Bob"
    assert item.published_at is not None


def test_parse_rss():
    items = parse_feed(RSS_XML)
    assert len(items) == 1
    item = items[0]
    assert item.title == "News & Views"
    assert item.url == "https://example.com/a"
    assert "<" not in item.summary
    assert "Hello world" in item.summary
    assert item.published_at is not None
