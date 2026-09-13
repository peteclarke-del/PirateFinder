from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from piratefinder.catalogue.search import CataloguePage, CatalogueRow
from piratefinder.finder import Finder, contents_summary, result_summary
from piratefinder.images import archives
from piratefinder.library.library import Library
from piratefinder.library.userdb import UserDatabase
from piratefinder.models import (
    Availability,
    Content,
    ContentKind,
    Disk,
    DiskKind,
    Platform,
    Query,
    QueueItem,
    ResultMode,
)
from piratefinder.settings import Settings
from tests.test_library_helpers import (
    CatalogueBuilder,
    SqlCatalogue,
    make_msa,
    make_st_image,
    store_module_available,
)


class SummaryTests(unittest.TestCase):
    def contents(self, titles: list[str]) -> list[Content]:
        return [
            Content(1, title, ContentKind.GAME, position) for position, title in enumerate(titles)
        ]

    def test_short_menus_are_listed_whole(self) -> None:
        self.assertEqual(contents_summary(self.contents(["A", "B"])), "A, B")
        self.assertEqual(contents_summary([]), "")

    def test_long_menus_keep_matches_and_menu_order(self) -> None:
        titles = [f"Game {n}" for n in range(1, 11)]
        self.assertEqual(
            contents_summary(self.contents(titles)),
            "Game 1, Game 2, Game 3, Game 4, Game 5, Game 6 and 4 more",
        )
        self.assertEqual(
            contents_summary(self.contents(titles), ["game 9"]),
            "Game 1, Game 2, Game 3, Game 4, Game 5, Game 9 and 4 more",
        )

    def test_repeated_titles_are_named_once_with_the_article_in_front(self) -> None:
        # A menu lists a game again for its doc and its cheat.
        contents = self.contents(
            ["New Zealand Story, The", "Rick Dangerous", "New Zealand Story, The", "rick dangerous"]
        )
        self.assertEqual(contents_summary(contents), "The New Zealand Story, Rick Dangerous")

    def test_single_disks_name_publisher_and_cracker_instead_of_repeating_the_title(self) -> None:
        single = Disk(
            1,
            "Rick Dangerous [cr Band]",
            Platform.AMIGA,
            DiskKind.SINGLE,
            publisher="Firebird",
            cracker="Band",
        )
        contents = self.contents(["Rick Dangerous"])
        self.assertEqual(result_summary(single, contents), "Firebird, cracked by Band")
        menu = replace(single, kind=DiskKind.MENU)
        self.assertEqual(result_summary(menu, contents), "Rick Dangerous")
        uncredited = replace(single, publisher="", cracker="")
        self.assertEqual(result_summary(uncredited, contents), "Rick Dangerous")


class FinderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-finder-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.files = self.folder / "files"
        self.files.mkdir()
        self.raw1 = make_st_image("one")
        builder = CatalogueBuilder(self.folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", series=("crew", "Crew"), number=1, contents=["Alpha", "Beta"])
        builder.disk(2, "Crew 2", series=("crew", "Crew"), number=2, contents=["Alpha 2"])
        builder.disk(3, "Crew 3", series=("crew", "Crew"), number=3, contents=["Alphabet"])
        builder.disk(4, "Pack 4", platform=Platform.AMIGA, kind=DiskKind.PACK)
        builder.image(10, 1, "Crew 1 [a].st", data=self.raw1, rank=1)
        builder.image(11, 1, "Crew 1.stx", rank=0)
        builder.image(12, 1, "Crew 1 [b].st", rank=0, bad=True)
        builder.image(20, 2, "Crew 2.st", rank=0)
        builder.location(1, 2, "slow-host", "https://slow.invalid/2.st", image_id=20, priority=50)
        builder.location(2, 2, "fast-host", "https://fast.invalid/2.st", image_id=20, priority=10)
        builder.location(3, 2, "fast-host", "https://fast.invalid/2b.st", image_id=21, priority=5)
        builder.location(4, 1, "fast-host", "https://fast.invalid/1.st", image_id=10, priority=1)
        builder.source("fast-host", "Fast Host")
        self.catalogue = SqlCatalogue(builder.close())
        self.addCleanup(self.catalogue.close)
        self.db = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.library = Library(self.db, self.catalogue, archives=archives)
        self.settings = Settings()
        self.finder = Finder(self.catalogue, self.library, self.settings)

    def test_providers_come_from_the_catalogue(self) -> None:
        self.assertEqual(self.finder.providers(), ["fast-host", "slow-host"])
        self.assertEqual(
            self.finder.provider_names(), {"fast-host": "Fast Host", "slow-host": "slow-host"}
        )
        self.settings.providers = {"slow-host": False}
        self.assertEqual(self.finder.enabled_providers(), ["fast-host"])

    def test_detail(self) -> None:
        detail = self.finder.detail(1)
        self.assertEqual(detail.disk.label, "Crew 1")
        self.assertEqual([c.title for c in detail.contents], ["Alpha", "Beta"])
        self.assertEqual(len(detail.images), 3)
        self.assertEqual(detail.availability, Availability.ONLINE)
        with self.assertRaises(LookupError):
            self.finder.detail(99)

    def test_corrections_apply(self) -> None:
        self.finder.save_details(1, {"label": "Crew 1 (corrected)", "platform": "amiga"})
        # The platform is not a field the user may correct, so it is left alone.
        self.assertEqual(self.finder.detail(1).disk.label, "Crew 1 (corrected)")
        self.assertEqual(self.finder.detail(1).disk.platform, Platform.ATARI_ST)
        page = CataloguePage([CatalogueRow(1, None, "", None, 0.0)], 1)
        finder = Finder(
            self.catalogue, self.library, self.settings, search_page=lambda *_a, **_k: page
        )
        (row,) = finder.search_page(Query(text="crew", mode=ResultMode.DISCS)).rows
        self.assertEqual(row.disk.label, "Crew 1 (corrected)")

    def test_sources_prefer_good_dumps_and_writable_formats(self) -> None:
        (self.files / "a.st").write_bytes(self.raw1)
        (self.files / "a.msa").write_bytes(make_msa(self.raw1))
        self.library.scan([self.files])
        # Pretend other dumps of the same disk are local too.
        for image_id, name in ((11, "b.stx"), (12, "c.st")):
            entry = self.db.entries(path=str(self.files / "a.st"))[0]
            path = str(self.files / name)
            self.db.store_file(
                path,
                1,
                1.0,
                [
                    entry.__class__(
                        path=path, format=Path(name).suffix[1:], image_id=image_id, disk_id=1
                    )
                ],
            )
        item = QueueItem(id="q", label="Crew 1", platform=Platform.ATARI_ST, disk_id=1)
        sources = self.finder.sources_for(item)
        labels = [Path(s.label).name if s.local else s.label for s in sources]
        # Writable dumps first even though the STX is ranked better, then the
        # STX, then the bad dump.
        self.assertEqual(labels, ["a.msa", "a.st", "b.stx", "c.st", "Fast Host (fast.invalid)"])
        self.assertEqual(sources[2].image.id, 11)

    def test_sources_for_a_chosen_image(self) -> None:
        item = QueueItem(id="q", label="Crew 2", platform=Platform.ATARI_ST, disk_id=2, image_id=20)
        sources = self.finder.sources_for(item)
        self.assertEqual([s.location.id for s in sources], [2, 1])
        self.assertTrue(all(s.platform is Platform.ATARI_ST for s in sources))
        whole_disk = QueueItem(id="r", label="Crew 2", platform=None, disk_id=2)
        self.assertEqual([s.location.id for s in self.finder.sources_for(whole_disk)], [3, 2, 1])

    def test_downloads_carry_every_dump_of_the_disc(self) -> None:
        # A download is checked against these when it has no checksum of its own.
        item = QueueItem(id="q", label="Crew 1", platform=None, disk_id=1)
        (download,) = self.finder.sources_for(item)
        self.assertEqual(sorted(dump.id for dump in download.dumps), [10, 11, 12])

    def test_sources_honour_provider_settings(self) -> None:
        item = QueueItem(id="q", label="Crew 2", platform=None, disk_id=2)
        self.settings.providers = {"fast-host": False}
        self.assertEqual(
            [s.location.provider for s in self.finder.sources_for(item)], ["slow-host"]
        )
        self.settings.online_enabled = False
        self.assertEqual(self.finder.sources_for(item), [])

    def test_sources_for_an_unmatched_file(self) -> None:
        (self.files / "Mystery.st").write_bytes(make_st_image("unmatched"))
        self.library.scan([self.files])
        local = self.library.search_unmatched("")[0]
        item = QueueItem(id="q", label="Mystery", platform=None, local=local)
        sources = self.finder.sources_for(item)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].local, local)
        self.assertEqual(sources[0].platform, Platform.ATARI_ST)

    def test_stats(self) -> None:
        stats = self.finder.stats()
        self.assertEqual(stats["catalogue_disks"], 4)
        self.assertEqual(stats["library_images"], 0)


@unittest.skipUnless(store_module_available(), "catalogue.store and search are not written yet")
class RealSearchTests(unittest.TestCase):
    def test_search_with_the_real_catalogue(self) -> None:
        from piratefinder.catalogue.store import Catalogue

        folder = Path(tempfile.mkdtemp(prefix="pf-finder-real-"))
        self.addCleanup(shutil.rmtree, folder, True)
        builder = CatalogueBuilder(folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", series=("crew", "Crew"), number=1, contents=["Zany Golf"])
        builder.disk(2, "Zany Golf [cr Band]", kind=DiskKind.SINGLE, contents=["Zany Golf"])
        builder.connection.execute(
            "UPDATE disks SET publisher = 'EA', cracker = 'Band' WHERE id = 2"
        )
        catalogue = Catalogue.open(builder.close())
        db = UserDatabase.open(folder / "user.sqlite")
        self.addCleanup(db.close)
        finder = Finder(catalogue, Library(db, catalogue), Settings())
        page = finder.search_page(Query(text="zany", mode=ResultMode.DISCS))
        results = {r.disk.id: r for r in page.rows}
        self.assertEqual(sorted(results), [1, 2])
        self.assertEqual((results[1].matched, results[1].summary), (("Zany Golf",), "Zany Golf"))
        # The single disk's row names its credits, with no title to put back in front.
        self.assertEqual((results[2].matched, results[2].summary), ((), "EA, cracked by Band"))

    def test_downloads_are_filed_by_type_and_crew(self) -> None:
        from piratefinder.catalogue.store import Catalogue

        folder = Path(tempfile.mkdtemp(prefix="pf-finder-archive-"))
        self.addCleanup(shutil.rmtree, folder, True)
        builder = CatalogueBuilder(folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", series=("crew-menu", "Crew Menu"), number=1, contents=["Zak"])
        builder.disk(2, "Zak [cr Band]", kind=DiskKind.SINGLE, contents=["Zak"])
        builder.connection.execute("UPDATE series SET group_name = 'The Crew'")
        builder.connection.execute("UPDATE disks SET cracker = 'Band' WHERE id = 2")
        catalogue = Catalogue.open(builder.close())
        db = UserDatabase.open(folder / "user.sqlite")
        self.addCleanup(db.close)
        finder = Finder(catalogue, Library(db, catalogue), Settings())
        self.assertEqual(finder.archive_folders(1), ("Games", "The Crew"))
        self.assertEqual(finder.archive_folders(2), ("Games", "Band"))
        self.assertEqual(finder.archive_folders(99), ("Games", "Unknown crew"))


if __name__ == "__main__":
    unittest.main()
