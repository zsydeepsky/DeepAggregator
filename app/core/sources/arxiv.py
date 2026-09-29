import xml.etree.ElementTree as ET
from datetime import datetime

from app.core.sources.base import ArchiveRequest, BaseSource, RawItem, http_get_text

NS = {"a": "http://www.w3.org/2005/Atom"}
DEFAULT_ENDPOINT = "https://export.arxiv.org/api/query"


def parse_arxiv_atom(text: str) -> list[RawItem]:
    root = ET.fromstring(text)
    items = []
    for entry in root.findall("a:entry", NS):
        entry_id = (entry.findtext("a:id", "", NS) or "").strip()
        published = (entry.findtext("a:published", "", NS) or "").strip()
        authors = [
            (author.findtext("a:name", "", NS) or "").strip()
            for author in entry.findall("a:author", NS)
        ]
        try:
            published_at = (
                datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
            )
        except ValueError:
            published_at = None
        items.append(
            RawItem(
                guid=entry_id,
                title=" ".join((entry.findtext("a:title", "", NS) or "").split()),
                summary=" ".join((entry.findtext("a:summary", "", NS) or "").split()),
                url=entry_id,
                author=", ".join(name for name in authors if name),
                published_at=published_at,
            )
        )
    return items


class ArxivSource(BaseSource):
    type = "arxiv"

    async def fetch(self) -> list[RawItem]:
        categories = self.config.get("categories", [])
        query = " OR ".join(f"cat:{c.strip()}" for c in categories if c.strip())
        if not query:
            query = self.config.get("query", "")
        params = {
            "search_query": query,
            "start": int(self.config.get("start", 0)),
            "max_results": int(self.config.get("max_results", 50)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        endpoint = self.source["url"].strip() or DEFAULT_ENDPOINT
        return parse_arxiv_atom(await http_get_text(endpoint, params=params))

    async def archive_requests(self, item: dict) -> list[ArchiveRequest]:
        url = item["url"]
        if "/abs/" in url:
            arxiv_id = url.rsplit("/abs/", 1)[1]
            pdf_url = url.replace("/abs/", "/pdf/")
            if pdf_url.startswith("http://"):
                pdf_url = "https://" + pdf_url[len("http://"):]
            return [
                ArchiveRequest(url=pdf_url, kind="pdf", filename=f"{arxiv_id}.pdf"),
                ArchiveRequest(url=url, kind="html", filename=f"{arxiv_id}.html"),
            ]
        return [ArchiveRequest(url=url, kind="auto")]

    def viewer(self, item: dict, assets: list[dict]) -> dict:
        for asset in assets:
            if asset["kind"] == "pdf":
                return {"kind": "pdf", "asset": asset}
        return super().viewer(item, assets)
