"""English Wikipedia articles for Atari ST and Amiga games, found through Wikidata.

Wikidata (CC0) links many games to their ids on the retro game sites and to
their English Wikipedia article. One SPARQL query at build time, sent to
query.wikidata.org with the builder's User-Agent and cached for 30 days,
fetches every item that has one of the ids in ``PROPERTIES`` and an English
article, with the item's English label.

The answer is used in two ways:

- ``articles(ctx).by_atari_legend`` maps an Atari Legend game id to its
  article. The Atari Legend importer attaches the article to the games on
  its menu disks by id, which is exact.
- ``collect`` emits the articles by title: for each platform, every
  normalised title (``libretro.title_key`` of the label and of the article
  title without its bracketed disambiguation) that belongs to exactly one
  item becomes a media-only record whose Wikipedia trivia names that title
  in ``content_title``. The merge gives it to every title of that name on
  the record's platform, so TOSEC single-game disks and compacts get the
  article too. A title shared by two items is left out rather than guessed.

The article text itself is fetched by the application when it is shown; the
catalogue holds only the article title (Wikipedia text is CC BY-SA 4.0).
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..context import BuildContext
from ..records import DiskRecord, SourceInfo, TriviaRecordIn
from .libretro import title_key

INFO = SourceInfo(
    id="wikidata",
    name="Wikidata",
    url="https://www.wikidata.org/",
    licence="CC0 1.0",
)
CONTENT_PRIORITY = 95

ENDPOINT = "https://query.wikidata.org/sparql"
MAX_AGE_DAYS = 30.0
# Wikidata properties holding a game's id on a site, and the platform whose
# titles an item with that id is matched against.
PROPERTIES = {
    "P4858": "atari-st",  # Atari Legend ID
    "P4671": "amiga",  # Hall of Light ID
    "P4846": "amiga",  # Lemon Amiga ID
    "P7683": "amiga",  # OpenRetro Game Database ID
}
ATARI_LEGEND = "P4858"
ENGLISH_WIKIPEDIA = "https://en.wikipedia.org/"
ARTICLE_PREFIX = ENGLISH_WIKIPEDIA + "wiki/"
WIKIPEDIA_SOURCE = "wikipedia"
WIKIPEDIA_LICENCE = "CC BY-SA 4.0"

QUERY = """SELECT ?item ?label ?property ?id ?article WHERE {
  VALUES ?property { %s }
  ?item ?property ?id .
  ?article schema:about ?item ;
           schema:isPartOf <%s> .
  OPTIONAL { ?item rdfs:label ?label . FILTER(LANG(?label) = "en") }
}"""


def query_url() -> str:
    properties = " ".join(f"wdt:{name}" for name in PROPERTIES)
    query = QUERY % (properties, ENGLISH_WIKIPEDIA)
    return f"{ENDPOINT}?{urllib.parse.urlencode({'format': 'json', 'query': query})}"


def article_title(url: str) -> str:
    """ "https://en.wikipedia.org/wiki/Xenon_2:_Megablast" -> "Xenon 2: Megablast"."""
    if not url.startswith(ARTICLE_PREFIX):
        return ""
    return urllib.parse.unquote(url[len(ARTICLE_PREFIX) :]).replace("_", " ").strip()


def article_url(title: str) -> str:
    return ARTICLE_PREFIX + urllib.parse.quote(title.replace(" ", "_"), safe="():,'!&-")


@dataclass(slots=True)
class Item:
    id: str
    article: str
    label: str = ""
    platforms: set[str] = field(default_factory=set)
    atari_legend: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Articles:
    items: dict[str, Item] = field(default_factory=dict)
    by_atari_legend: dict[str, str] = field(default_factory=dict)

    def by_title(self, platform: str) -> dict[str, str]:
        """Normalised title -> article, for titles that name a single item."""
        owners: dict[str, set[str]] = {}
        for item in self.items.values():
            if platform not in item.platforms:
                continue
            for name in (item.label, item.article):
                key = title_key(name)
                if key:
                    owners.setdefault(key, set()).add(item.id)
        return {
            key: self.items[next(iter(ids))].article
            for key, ids in sorted(owners.items())
            if len(ids) == 1
        }


def parse(document: dict) -> Articles:
    """The SPARQL JSON answer as items; an id shared by several items is dropped."""
    found = Articles()
    legend: dict[str, set[str]] = {}
    for binding in document.get("results", {}).get("bindings", []):
        item_uri = binding.get("item", {}).get("value", "")
        article = article_title(binding.get("article", {}).get("value", ""))
        prop = binding.get("property", {}).get("value", "").rsplit("/", 1)[-1]
        if not item_uri or not article or prop not in PROPERTIES:
            continue
        item_id = item_uri.rsplit("/", 1)[-1]
        item = found.items.setdefault(item_id, Item(id=item_id, article=article))
        item.label = item.label or binding.get("label", {}).get("value", "").strip()
        item.platforms.add(PROPERTIES[prop])
        if prop == ATARI_LEGEND:
            site_id = binding.get("id", {}).get("value", "").strip()
            if site_id:
                item.atari_legend.add(site_id)
                legend.setdefault(site_id, set()).add(item_id)
    found.by_atari_legend = {
        site_id: found.items[next(iter(ids))].article
        for site_id, ids in sorted(legend.items())
        if len(ids) == 1
    }
    return found


def articles(ctx: BuildContext) -> Articles:
    """Every Wikidata game item with an English article, fetched once and cached."""
    path = ctx.input(INFO.id) or ctx.fetch(
        query_url(), name="wikidata-games.json", max_age_days=MAX_AGE_DAYS
    )
    with path.open("rb") as handle:
        return parse(json.load(handle))


def records(found: Articles, log=lambda message: None) -> Iterator[DiskRecord]:
    """One media-only record per platform and title, attached by normalised title."""
    for platform in sorted(set(PROPERTIES.values())):
        titles = found.by_title(platform)
        log(f"{INFO.id}: {len(titles)} unambiguous {platform} titles with an article")
        for key, article in titles.items():
            yield DiskRecord(
                source=INFO.id,
                platform=platform,
                kind="single",
                trivia=[
                    TriviaRecordIn(
                        kind="wikipedia",
                        text=article,
                        source=WIKIPEDIA_SOURCE,
                        url=article_url(article),
                        licence=WIKIPEDIA_LICENCE,
                        content_title=key,
                    )
                ],
            )


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    found = articles(ctx)
    ctx.log(
        f"{INFO.id}: {len(found.items)} items with an English article, "
        f"{len(found.by_atari_legend)} Atari Legend ids"
    )
    yield from records(found, ctx.log)
