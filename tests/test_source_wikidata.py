"""The Wikidata importer, run offline on a small synthetic SPARQL answer."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from catalogue_builder.context import BuildContext, OfflineError
from catalogue_builder.series import SeriesRegistry
from catalogue_builder.sources import wikidata

ANSWER = Path(__file__).parent / "fixtures" / "wikidata" / "answer.json"


def parsed() -> wikidata.Articles:
    return wikidata.parse(json.loads(ANSWER.read_text()))


class ParseTest(unittest.TestCase):
    def test_items_with_an_english_article(self) -> None:
        found = parsed()
        # Q8 has another property and Q10 only a German article.
        self.assertEqual(
            sorted(found.items, key=lambda name: int(name[1:])),
            ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q9", "Q11"],
        )
        rick = found.items["Q1"]
        self.assertEqual((rick.article, rick.label), ("Rick Dangerous", "Rick Dangerous"))
        self.assertEqual(rick.platforms, {"atari-st", "amiga"})
        self.assertEqual(found.items["Q11"].article, "The Bard's Tale (1985 video game)")

    def test_atari_legend_ids(self) -> None:
        # Id 999 belongs to two items, so it is left out.
        self.assertEqual(
            parsed().by_atari_legend, {"329": "Rick Dangerous", "360": "Xenon 2: Megablast"}
        )

    def test_titles_that_name_one_item(self) -> None:
        amiga = parsed().by_title("amiga")
        self.assertEqual(
            amiga,
            {
                "lemmings": "Lemmings (video game)",
                "rick dangerous": "Rick Dangerous",
                "the bards tale": "The Bard's Tale (1985 video game)",
                "the chaos engine": "The Chaos Engine",
                "xenon 2 megablast": "Xenon 2: Megablast",
            },
        )  # "Hunter" names two items
        self.assertEqual(
            sorted(parsed().by_title("atari-st")),
            ["rick dangerous", "twin one", "twin two", "xenon 2 megablast"],
        )

    def test_article_addresses(self) -> None:
        self.assertEqual(
            wikidata.article_url("Xenon 2: Megablast"),
            "https://en.wikipedia.org/wiki/Xenon_2:_Megablast",
        )
        title = "The Bard's Tale (1985 video game)"
        self.assertEqual(wikidata.article_title(wikidata.article_url(title)), title)
        self.assertEqual(wikidata.article_title("https://de.wikipedia.org/wiki/X"), "")

    def test_one_query_for_every_property(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(wikidata.query_url()).query)
        self.assertEqual(query["format"], ["json"])
        for name in ("P4858", "P4671", "P4846", "P7683"):
            self.assertIn(f"wdt:{name}", query["query"][0])


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.messages: list[str] = []
        self.ctx = BuildContext(
            cache_dir=self.temp,
            series=SeriesRegistry.load(),
            offline=True,
            log=self.messages.append,
        )

    def cache(self) -> None:
        target = self.ctx.cache_path(wikidata.query_url(), "wikidata-games.json")
        target.parent.mkdir(parents=True)
        shutil.copy(ANSWER, target)

    def test_media_only_records_of_articles_by_title(self) -> None:
        self.cache()
        records = list(wikidata.collect(self.ctx))
        self.assertEqual(len(records), 9)
        by_title = {(r.platform, r.trivia[0].content_title): r for r in records}
        record = by_title[("amiga", "the chaos engine")]
        self.assertIsNone(record.key)
        self.assertEqual((record.images, record.contents, record.media), ([], [], []))
        self.assertEqual(record.source, "wikidata")
        [article] = record.trivia
        self.assertEqual((article.kind, article.text), ("wikipedia", "The Chaos Engine"))
        self.assertEqual(article.url, "https://en.wikipedia.org/wiki/The_Chaos_Engine")
        self.assertEqual((article.source, article.licence), ("wikipedia", "CC BY-SA 4.0"))
        self.assertIn(("atari-st", "rick dangerous"), by_title)
        self.assertTrue(any("9 items with an English article" in m for m in self.messages))

    def test_articles_for_the_atari_legend_importer(self) -> None:
        self.cache()
        self.assertEqual(wikidata.articles(self.ctx).by_atari_legend["329"], "Rick Dangerous")

    def test_a_local_answer_replaces_the_query(self) -> None:
        self.ctx.inputs["wikidata"] = ANSWER
        self.assertEqual(len(wikidata.articles(self.ctx).items), 9)

    def test_the_query_is_fetched_when_not_cached(self) -> None:
        with self.assertRaises(OfflineError) as raised:
            wikidata.articles(self.ctx)
        self.assertIn("query.wikidata.org/sparql", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
