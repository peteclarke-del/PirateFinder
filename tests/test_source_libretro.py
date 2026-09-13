"""The libretro-thumbnails importer, run offline on small synthetic git tree listings."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.context import BuildContext, OfflineError
from catalogue_builder.series import SeriesRegistry
from catalogue_builder.sources import libretro

FIXTURES = Path(__file__).parent / "fixtures" / "libretro"
ST, AMIGA = libretro.REPOSITORIES
RAW = "https://raw.githubusercontent.com/libretro-thumbnails"


def tree(repo: libretro.Repository) -> dict:
    return json.loads((FIXTURES / f"tree-{repo.name}.json").read_text())


class TitleKeyTest(unittest.TestCase):
    def test_title_keys(self) -> None:
        self.assertEqual(libretro.title_key("Chaos Engine, The (Europe)"), "the chaos engine")
        self.assertEqual(libretro.title_key("Rick Dangerous v1.1 [cr SR]"), "rick dangerous")
        self.assertEqual(libretro.title_key("Xenon 2 - Megablast"), "xenon 2 megablast")
        self.assertEqual(libretro.title_key("Dungeons _ Dragons"), "dungeons and dragons")
        self.assertEqual(libretro.title_key("'Nam 1965-1975"), "nam 1965 1975")
        self.assertEqual(libretro.title_key("(Untitled)"), "")

    def test_image_names(self) -> None:
        self.assertEqual(libretro.image_name("A _ B (1990)(X)"), "A & B (1990)(X).st")
        self.assertEqual(libretro.image_name("Geo_Monde (1993)(X)"), "Geo_Monde (1993)(X).st")


class PicturesTest(unittest.TestCase):
    def test_only_pictures_in_the_wanted_folders(self) -> None:
        found = libretro.pictures(tree(ST))
        self.assertEqual(
            sorted((p.folder, p.name) for p in found),
            [
                (
                    "Named_Boxarts",
                    "Rick Dangerous (1989)(Core Design - Firebird)[cr Medway Boys][t]",
                ),
                ("Named_Snaps", "Black _ White"),
                ("Named_Snaps", "Dungeons _ Dragons (1990)(SSI)[cr Elite]"),
                ("Named_Snaps", "Rick Dangerous (1989)(Core Design - Firebird)[cr Medway Boys][t]"),
                ("Named_Snaps", "Xenon 2 - Megablast (1989)(Image Works)[cr #1]"),
                ("Named_Snaps", "Zynaps"),
                (
                    "Named_Titles",
                    "Rick Dangerous (1989)(Core Design - Firebird)[cr Medway Boys][t]",
                ),
            ],
        )


class AtariStTest(unittest.TestCase):
    def setUp(self) -> None:
        self.messages: list[str] = []
        self.records = libretro.records(ST, tree(ST), self.messages.append)
        self.by_name = {record.media[0].image_name: record for record in self.records}

    def test_media_only_records_by_tosec_image_name(self) -> None:
        self.assertEqual(
            sorted(self.by_name),
            [
                "Dungeons & Dragons (1990)(SSI)[cr Elite].st",
                "Rick Dangerous (1989)(Core Design - Firebird)[cr Medway Boys][t].st",
                "Xenon 2 - Megablast (1989)(Image Works)[cr #1].st",
            ],
        )
        for record in self.records:
            self.assertIsNone(record.key)
            self.assertEqual((record.images, record.contents), ([], []))
            self.assertEqual((record.source, record.platform), ("libretro-thumbnails", "atari-st"))

    def test_every_kind_of_picture_in_rank_order(self) -> None:
        name = "Rick Dangerous (1989)(Core Design - Firebird)[cr Medway Boys][t].st"
        media = self.by_name[name].media
        self.assertEqual(
            [(m.kind, m.rank, m.credit) for m in media],
            [
                ("snap", 40, "Snap: libretro-thumbnails"),
                ("title", 45, "Title screen: libretro-thumbnails"),
                ("boxart", 50, "Box art: libretro-thumbnails"),
            ],
        )
        self.assertEqual(
            media[0].url,
            f"{RAW}/Atari_-_ST/master/Named_Snaps/Rick%20Dangerous%20%281989%29%28Core%20Design"
            "%20-%20Firebird%29%5Bcr%20Medway%20Boys%5D%5Bt%5D.png",
        )
        self.assertEqual(media[0].page_url, "https://github.com/libretro-thumbnails/Atari_-_ST")
        self.assertEqual({m.title_key for m in media}, {""})

    def test_characters_that_break_an_address_are_quoted(self) -> None:
        [media] = self.by_name["Xenon 2 - Megablast (1989)(Image Works)[cr #1].st"].media
        self.assertTrue(
            media.url.endswith(
                "/Xenon%202%20-%20Megablast%20%281989%29%28Image%20Works%29%5Bcr%20%231%5D.png"
            )
        )

    def test_counts_are_logged(self) -> None:
        self.assertEqual(
            self.messages,
            [
                "libretro-thumbnails: Atari_-_ST: 7 pictures, 3 snap, 1 title, 1 boxart "
                "for 3 image names"
            ],
        )


class AmigaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.messages: list[str] = []
        self.records = libretro.records(AMIGA, tree(AMIGA), self.messages.append)
        self.by_key = {record.media[0].title_key: record for record in self.records}

    def test_media_only_records_by_title(self) -> None:
        self.assertEqual(
            sorted(self.by_key),
            ["4d sports driving and master tracks i", "rick dangerous", "the chaos engine"],
        )
        for record in self.records:
            self.assertEqual((record.key, record.platform), (None, "amiga"))
            self.assertEqual({m.image_name for m in record.media}, {""})

    def test_one_picture_of_each_kind_the_plainest_name_first(self) -> None:
        media = self.by_key["rick dangerous"].media
        self.assertEqual(
            [(m.kind, m.url.rsplit("/", 1)[-1]) for m in media],
            [
                ("snap", "Rick%20Dangerous.png"),
                ("title", "Rick%20Dangerous%20%28Europe%29.png"),
                ("boxart", "Rick%20Dangerous%20%28Europe%29.png"),
            ],
        )

    def test_a_truncated_listing_is_reported(self) -> None:
        self.assertIn("truncated", self.messages[0])


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.ctx = BuildContext(
            cache_dir=self.temp, series=SeriesRegistry.load(), offline=True, log=lambda m: None
        )

    def cache(self, repo: libretro.Repository) -> None:
        url = libretro.TREE_URL.format(repo=repo.name, branch=libretro.BRANCH)
        target = self.ctx.cache_path(url, f"tree-{repo.name}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / f"tree-{repo.name}.json", target)

    def test_one_listing_per_repository(self) -> None:
        self.cache(ST)
        self.cache(AMIGA)
        records = list(libretro.collect(self.ctx))
        self.assertEqual(len(records), 6)
        self.assertEqual(
            libretro.TREE_URL.format(repo="Atari_-_ST", branch="master"),
            "https://api.github.com/repos/libretro-thumbnails/Atari_-_ST/git/trees/master"
            "?recursive=1",
        )

    def test_a_missing_listing_is_fetched(self) -> None:
        self.cache(ST)
        with self.assertRaises(OfflineError) as raised:
            list(libretro.collect(self.ctx))
        self.assertIn("Commodore_-_Amiga", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
