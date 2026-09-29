from datetime import datetime, timezone

import feedparser

from app.core.sources.base import BaseSource, RawItem, clean_text, http_get_text


def parse_feed(text: str) -> list[RawItem]:
    parsed = feedparser.parse(text)
    items = []
    for entry in parsed.entries:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            stamp = getattr(entry, key, None)
            if stamp:
                published = datetime(*stamp[:6], tzinfo=timezone.utc)
                break
        items.append(
            RawItem(
                guid=getattr(entry, "id", "") or getattr(entry, "link", ""),
                title=clean_text(getattr(entry, "title", ""), 500),
                summary=clean_text(
                    getattr(entry, "summary", "") or getattr(entry, "description", "")
                ),
                url=getattr(entry, "link", ""),
                author=clean_text(getattr(entry, "author", ""), 200),
                published_at=published,
            )
        )
    return items


class RssSource(BaseSource):
    type = "rss"

    async def fetch(self) -> list[RawItem]:
        return parse_feed(await http_get_text(self.source["url"]))
