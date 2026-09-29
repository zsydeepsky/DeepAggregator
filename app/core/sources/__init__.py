from app.core.sources.arxiv import ArxivSource
from app.core.sources.base import ArchiveRequest, BaseSource, RawItem
from app.core.sources.bilibili import BilibiliSource
from app.core.sources.reddit import RedditSource
from app.core.sources.rss import RssSource
from app.core.sources.youtube import YouTubeSource

SOURCES = {
    "rss": RssSource,
    "arxiv": ArxivSource,
    "reddit": RedditSource,
    "youtube": YouTubeSource,
    "bilibili": BilibiliSource,
}


def get_source(source, app_settings=None) -> BaseSource:
    return SOURCES[source["type"]](source, app_settings)
