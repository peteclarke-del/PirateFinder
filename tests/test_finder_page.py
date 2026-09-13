"""The Find screen and details pane API of Finder, and the virus rules for sources.

The catalogue is built with the real schema; the page search is a fake that
records its arguments, and one test runs the real catalogue search and store
end to end. Images are synthetic.
"""

from __future__ import annotations

import sqlite3
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

from catalogue_builder.merge import SourceBatch, merge_records, write_catalogue
from catalogue_builder.records import (
    ContentRecord,
    CrewRecord,
    DiskRecord,
    ImageRecordIn,
    LocationRecord,
    MediaRecordIn,
    SourceInfo,
    TriviaRecordIn,
)
from catalogue_builder.series import GroupRegistry, SeriesDef, SeriesRegistry
from piratefinder.catalogue.store import Catalogue
from piratefinder.finder import Finder
from piratefinder.images.inspect import inspect_bytes
from piratefinder.library.library import Library
from piratefinder.library.userdb import UserDatabase
from piratefinder.models import (
    Availability,
    ContentKind,
    Facets,
    LocalFile,
    MediaItem,
    Platform,
    Query,
    QueueItem,
    ResultMode,
    TriviaItem,
    VirusStatus,
)
from piratefinder.settings import Settings
from tests.test_images_virus import VirusEnvironment, st_body, st_infected, st_signature_toml
from tests.test_library_helpers import CatalogueBuilder, SqlCatalogue, make_st_image


@dataclass
class Row:
    disk_id: int
    content_id: int | None = None
    title: str = ""
    content_kind: ContentKind | None = None
    score: float = 0.0
    matched: tuple[str, ...] = ()


@dataclass
class Page:
    rows: list[Row] = field(default_factory=list)
    total: int = 0


class FakePageSearch:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.calls: list[tuple[Query, set[int], list[str]]] = []
        self.overrides = []

    def __call__(self, catalogue, query, *, local_disks=(), providers=(), overrides=None):
        self.calls.append((query, set(local_disks), list(providers)))
        self.overrides.append(overrides)
        return self.page


class FakeMedia:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.titles: list[str] = []
        self.fetched: list[MediaItem] = []

    def wikipedia_summary(self, title: str, *, cancel=None) -> TriviaItem | None:
        self.titles.append(title)
        if title == "Missing Article":
            return None
        return TriviaItem("summary", f"About {title}.", "Wikipedia", title=title)

    def fetch(self, item: MediaItem, *, cancel=None) -> Path | None:
        self.fetched.append(item)
        return Path("/cache/picture.png")


class FinderPageTests(VirusEnvironment, unittest.TestCase):
    def setUp(self) -> None:
        self.folder = self.use_environment()
        self.body = st_body()
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))
        self.files = self.folder / "files"
        self.files.mkdir()
        self.flagged = make_st_image("flagged dump")
        self.clean = make_st_image("clean dump")
        self.bad = make_st_image("bad dump")
        self.infected = st_infected(make_st_image("boot virus"), self.body)
        self.infected_a2 = st_infected(make_st_image("four a2"), self.body)
        builder = CatalogueBuilder(self.folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", series=("crew", "Crew"), number=1, contents=["Alpha", "Beta"])
        builder.disk(
            2, "Crew 2", series=("crew", "Crew"), number=2, contents=["New Zealand Story, The"]
        )
        builder.disk(3, "Pack 3", platform=Platform.AMIGA, crew="Nobody")
        builder.disk(4, "Crew 4", series=("crew", "Crew"), number=4, crew="Automation")
        builder.image(10, 1, "Crew 1 [v Ghost].st", data=self.flagged, rank=0, virus="Ghost")
        builder.image(11, 1, "Crew 1 [a].st", data=self.clean, rank=1)
        builder.image(12, 1, "Crew 1 [b].st", data=self.bad, rank=2, bad=True)
        builder.image(20, 2, "Crew 2 [v Ghost].st", data=make_st_image("two"), virus="Ghost")
        builder.image(40, 4, "Crew 4.st", data=self.infected, rank=0)
        builder.image(41, 4, "Crew 4 [a].st", data=make_st_image("four a"), rank=1)
        builder.image(42, 4, "Crew 4 [a2].st", data=self.infected_a2, rank=2)
        builder.location(1, 1, "host", "https://host.invalid/10.st", image_id=10, priority=1)
        builder.location(2, 1, "host", "https://host.invalid/11.st", image_id=11, priority=50)
        builder.location(3, 1, "host", "https://host.invalid/12.st", image_id=12, priority=1)
        builder.location(4, 2, "host", "https://host.invalid/20.st", image_id=20)
        # Disc 5's only known dump is flagged, and a second host has a copy the
        # catalogue cannot tie to a dump. Disc 6 has a flagged ST dump and a
        # clean STX, and only the STX is hosted.
        builder.disk(5, "Intros 5", platform=Platform.AMIGA)
        builder.image(50, 5, "Intros 5 [v Ghost].adf", virus="Ghost")
        builder.location(5, 5, "host", "https://host.invalid/50.adf", image_id=50, priority=20)
        builder.location(6, 5, "host", "https://host.invalid/5.zip", priority=50)
        builder.disk(6, "Crew 6", series=("crew", "Crew"), number=6)
        builder.image(60, 6, "Crew 6 [v Ghost].st", virus="Ghost", rank=0)
        builder.image(61, 6, "Crew 6.stx", rank=1)
        builder.location(7, 6, "host", "https://host.invalid/61.stx", image_id=61)
        self.alpha = builder.content_id(1, 0)
        builder.media(
            1, "https://pics.invalid/alpha.png", kind="title", content_id=self.alpha, rank=1
        )
        builder.media(1, "https://pics.invalid/menu.png", kind="menu", rank=50)
        builder.trivia(1, "fact", "Released at a copy party.")
        builder.trivia(1, "note", "Alpha has a trainer.", content_id=self.alpha)
        builder.trivia(1, "wikipedia", "Crew (group)")
        builder.trivia(1, "wikipedia", "Alpha (game)", content_id=self.alpha)
        builder.trivia(1, "wikipedia", "Beta (game)", content_id=builder.content_id(1, 1))
        # An Amiga group of the same name comes first; disc 4 must not get it.
        builder.crew("Automation", platform=Platform.AMIGA, notes="An Amiga demo group.")
        builder.crew(
            "Automation", disks=[4], notes="An Atari ST crew.", wikipedia="Automation (group)"
        )
        self.catalogue = SqlCatalogue(builder.close())
        self.addCleanup(self.catalogue.close)
        self.db = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.library = Library(self.db, self.catalogue)
        self.settings = Settings(download_folder=str(self.folder / "downloads"))
        self.media = FakeMedia()

    def finder(self, page: Page | None = None) -> tuple[Finder, FakePageSearch]:
        search = FakePageSearch(page or Page())
        finder = Finder(
            self.catalogue,
            self.library,
            self.settings,
            search_page=search,
            facets=lambda catalogue, overrides: Facets(crews=(("Automation", 1),)),
            media=self.media,
        )
        return finder, search

    def scan(self, **files: bytes) -> None:
        for name, data in files.items():
            (self.files / name).write_bytes(data)
        self.assertEqual(self.library.scan([self.files]).errors, ())

    # Search ------------------------------------------------------------------

    def test_disc_rows_carry_contents_availability_and_virus(self) -> None:
        self.scan(**{"four.st": self.infected})
        rows = [Row(1, matched=("beta",)), Row(2), Row(3), Row(4), Row(99)]
        finder, search = self.finder(Page(rows, total=250))
        query = Query(text="crew", mode=ResultMode.DISCS, page=1)
        page = finder.search_page(query)
        self.assertEqual(page.total, 250)
        self.assertIs(page.query, query)
        self.assertEqual(search.calls, [(query, {4}, ["host"])])
        by_disk = {row.disk.id: row for row in page.rows}
        self.assertEqual(sorted(by_disk), [1, 2, 3, 4], "a disc the catalogue lacks is left out")
        self.assertEqual(by_disk[1].summary, "Alpha, Beta")
        self.assertEqual(by_disk[1].matched, ("beta",))
        self.assertEqual(by_disk[2].summary, "The New Zealand Story")
        self.assertEqual(
            (by_disk[1].title, by_disk[1].content_id, by_disk[1].key), ("", None, "disk:1")
        )
        self.assertEqual(
            {disk_id: row.availability for disk_id, row in by_disk.items()},
            {
                1: Availability.ONLINE,
                2: Availability.ONLINE,
                3: Availability.MISSING,
                4: Availability.LOCAL,
            },
        )
        # Disc 1 has a clean dump, disc 2 only a flagged one, disc 4 a local boot virus.
        self.assertEqual(
            {disk_id: row.virus for disk_id, row in by_disk.items()},
            {1: "", 2: "Ghost", 3: "", 4: "Synthetic Ghost"},
        )

    def test_title_rows(self) -> None:
        rows = [
            Row(
                2,
                content_id=7,
                title="New Zealand Story, The",
                content_kind=ContentKind.GAME,
                matched=("New Zealand Story, The",),
            ),
            Row(3, content_id=None, title="Pack 3"),
            Row(1, content_id=8, title="Beta", content_kind="demo"),
        ]
        finder, _search = self.finder(Page(rows, total=3))
        page = finder.search_page(Query(text="new"))
        first, disc, beta = page.rows
        self.assertEqual(
            (first.title, first.content_id, first.content_kind),
            ("The New Zealand Story", 7, ContentKind.GAME),
        )
        self.assertEqual(
            (first.summary, first.matched, first.key), ("", ("The New Zealand Story",), "title:7")
        )
        self.assertEqual(
            (disc.title, disc.content_id, disc.content_kind, disc.key),
            ("Pack 3", None, None, "disk:3"),
        )
        self.assertEqual(beta.content_kind, ContentKind.DEMO)
        self.assertEqual(first.virus, "Ghost")

    def test_a_local_copy_of_a_flagged_dump_shows_the_flag(self) -> None:
        self.scan(**{"flagged.st": self.flagged})
        finder, _search = self.finder(Page([Row(1)], 1))
        self.assertEqual(finder.search_page(Query(mode=ResultMode.DISCS)).rows[0].virus, "Ghost")
        self.scan(**{"clean.st": self.clean})
        self.assertEqual(finder.search_page(Query(mode=ResultMode.DISCS)).rows[0].virus, "")

    def test_the_row_and_the_pane_describe_the_dump_the_writer_would_use(self) -> None:
        finder, _search = self.finder(Page([Row(disk_id) for disk_id in range(1, 7)], 6))

        def shown() -> dict[int, tuple[str, str]]:
            """Each disc's row warning, and the virus its details pane reports."""
            found = {}
            for row in finder.search_page(Query(mode=ResultMode.DISCS)).rows:
                report = finder.detail(row.disk.id).virus
                found[row.disk.id] = (row.virus, report.name if report and report.infected else "")
            return found

        for disk_id, (row, pane) in shown().items():
            with self.subTest(disc=disk_id):
                self.assertEqual(row, pane)
        # The writer would take the untied download of disc 5 and the clean STX of disc 6.
        self.assertEqual((shown()[5], shown()[6]), (("", ""), ("", "")))
        self.settings.online_enabled = False  # nothing to download: the best known dumps
        self.assertEqual((shown()[5], shown()[6]), (("Ghost", "Ghost"), ("Ghost", "Ghost")))
        self.settings.online_enabled = True
        self.scan(**{"flagged.st": self.flagged, "four.st": self.infected})
        for disk_id, (row, pane) in shown().items():
            with self.subTest(disc=disk_id, scanned=True):
                self.assertEqual(row, pane)
        self.assertEqual(shown()[1], ("Ghost", "Ghost"))
        self.assertEqual(shown()[4], ("Synthetic Ghost", "Synthetic Ghost"))

    def test_facets(self) -> None:
        finder, _search = self.finder()
        self.assertEqual(finder.facets().crews, (("Automation", 1),))

    # Sources -----------------------------------------------------------------

    def test_sources_prefer_dumps_without_a_virus(self) -> None:
        finder, _search = self.finder()
        item = QueueItem(id="1", label="Crew 1", platform=Platform.ATARI_ST, disk_id=1)
        online = [source.image.id for source in finder.sources_for(item)]
        self.assertEqual(online, [11, 10, 12], "clean, then flagged, then bad")
        self.scan(**{"a-flagged.st": self.flagged, "b-clean.st": self.clean, "c-bad.st": self.bad})
        local = [source.image.id for source in finder.sources_for(item) if source.local]
        self.assertEqual(local, [11, 10, 12])

    def test_a_local_boot_virus_ranks_after_a_clean_copy(self) -> None:
        self.scan(**{"a-infected.st": self.infected, "b-alternate.st": make_st_image("four a")})
        finder, _search = self.finder()
        item = QueueItem(id="4", label="Crew 4", platform=Platform.ATARI_ST, disk_id=4)
        self.assertEqual([source.image.id for source in finder.sources_for(item)], [41, 40])

    def test_clean_alternates(self) -> None:
        finder, _search = self.finder()
        self.assertEqual([record.id for record in finder.clean_alternates(1)], [11])
        self.assertEqual(finder.clean_alternates(2), [])

    # Detail ------------------------------------------------------------------

    def test_detail_has_pictures_facts_and_crew(self) -> None:
        finder, _search = self.finder()
        detail = finder.detail(1)
        self.assertEqual(
            [item.url.rsplit("/", 1)[1] for item in detail.media], ["menu.png", "alpha.png"]
        )
        self.assertEqual(
            [item.text for item in detail.trivia],
            ["Released at a copy party.", "Alpha has a trainer."],
        )
        self.assertIsNone(detail.crew)
        self.assertIsNone(detail.virus, "the writer would take the clean online dump")
        self.assertIsNone(detail.write_local)
        four = finder.detail(4)
        self.assertEqual(
            (four.crew.name, four.crew.notes, four.crew.wikipedia),
            ("Automation", "An Atari ST crew.", "Automation (group)"),
        )

    def test_a_corrected_crew_leaves_the_catalogues_history_out(self) -> None:
        finder, _search = self.finder()
        finder.save_details(4, {"crew": "Someone Else"})
        detail = finder.detail(4)
        self.assertEqual((detail.disk.crew, detail.crew), ("Someone Else", None))

    def test_detail_reports_a_local_boot_virus(self) -> None:
        self.scan(**{"four.st": self.infected})
        finder, _search = self.finder()
        report = finder.detail(4).virus
        self.assertEqual(
            (report.status, report.name, report.removable),
            (VirusStatus.VIRUS, "Synthetic Ghost", True),
        )

    def test_detail_names_the_file_the_writer_would_use(self) -> None:
        # Both copies carry the boot virus. The better dump is written, so that is
        # the file the pane describes and offers to clean, whichever is listed first.
        self.scan(**{"a-second.st": self.infected_a2, "b-main.st": self.infected})
        finder, _search = self.finder()
        detail = finder.detail(4)
        listed = [(Path(local.path).name, local.virus) for local in detail.local_files]
        self.assertEqual(
            listed, [("a-second.st", "Synthetic Ghost"), ("b-main.st", "Synthetic Ghost")]
        )
        item = QueueItem(id="4", label="Crew 4", platform=Platform.ATARI_ST, disk_id=4)
        self.assertEqual(detail.write_local, finder.sources_for(item)[0].local)
        self.assertEqual(Path(detail.write_local.path).name, "b-main.st")
        self.assertEqual(detail.virus, finder.local_virus_report(detail.write_local))

    def test_a_library_file_report_reads_the_file_and_adds_the_catalogue_flag(self) -> None:
        self.scan(**{"flagged.st": self.flagged, "four.st": self.infected})
        finder, _search = self.finder()
        (flagged,) = self.library.files_for_image(10)
        (infected,) = self.library.files_for_image(40)
        report = finder.local_virus_report(flagged)
        self.assertEqual((report.status, report.name), (VirusStatus.FLAGGED, "Ghost"))
        report = finder.local_virus_report(infected)
        self.assertEqual(
            (report.status, report.name, report.removable),
            (VirusStatus.VIRUS, "Synthetic Ghost", True),
        )

    def test_a_member_report_reads_the_image_under_its_own_name(self) -> None:
        self.scan(**{"four.st": self.infected})
        finder, _search = self.finder()
        (infected,) = self.library.files_for_image(40)
        member = replace(infected, path="/nas/set.7z", member="inner.zip::menus\\Four.ST")
        with (
            mock.patch.object(self.library, "read_bytes", return_value=self.infected),
            mock.patch("piratefinder.finder.inspect_bytes", wraps=inspect_bytes) as inspected,
        ):
            report = finder.local_virus_report(member)
        inspected.assert_called_once_with(self.infected, "Four.ST")
        self.assertEqual(report.name, "Synthetic Ghost")

    def test_detail_reports_a_flag_and_offers_the_clean_alternate(self) -> None:
        finder, _search = self.finder()
        report = finder.detail(2).virus
        self.assertEqual(
            (report.status, report.name, report.source), (VirusStatus.FLAGGED, "Ghost", "TOSEC")
        )
        self.assertNotIn("clean dump of this disc", report.explanation)
        self.scan(**{"flagged.st": self.flagged})
        local = finder.detail(1).virus
        self.assertEqual(local.status, VirusStatus.FLAGGED)
        self.assertIn("Crew 1 [a].st, which can be written instead", local.explanation)

    def test_detail_falls_back_to_the_scan_when_the_file_is_gone(self) -> None:
        self.scan(**{"four.st": self.infected})
        (self.files / "four.st").unlink()
        finder, _search = self.finder()
        report = finder.detail(4).virus
        self.assertEqual(
            (report.status, report.name, report.removable),
            (VirusStatus.VIRUS, "Synthetic Ghost", False),
        )
        self.assertIn("cannot be read now", report.explanation)

    def test_an_older_catalogue_without_the_new_tables(self) -> None:
        class Old:
            def __getattr__(self, name):
                if name in ("media", "trivia", "crew_for_disk"):
                    raise AttributeError(name)
                return getattr(catalogue, name)

        catalogue = self.catalogue
        finder = Finder(Old(), self.library, self.settings, media=self.media)
        detail = finder.detail(4)
        self.assertEqual((detail.media, detail.trivia, detail.crew), ((), (), None))

        class Broken:
            def __getattr__(self, name):
                if name in ("media", "trivia", "crew_for_disk"):

                    def fail(*arguments):
                        raise sqlite3.OperationalError(f"no such table: {name}")

                    return fail
                return getattr(catalogue, name)

        broken = Finder(Broken(), self.library, self.settings).detail(4)
        self.assertEqual((broken.media, broken.trivia, broken.crew), ((), (), None))

    # Summaries and pictures --------------------------------------------------------

    def test_summaries_for_a_title_the_disc_and_the_crew(self) -> None:
        finder, _search = self.finder()
        found = finder.summaries(1, self.alpha)
        self.assertEqual(
            [(item.title, item.content_id) for item in found],
            [("Alpha (game)", self.alpha), ("Crew (group)", None)],
        )
        self.assertEqual(
            self.media.titles, ["Alpha (game)", "Crew (group)"], "Beta is another title"
        )
        self.media.titles.clear()
        crew_disc = finder.summaries(4)
        self.assertEqual([item.title for item in crew_disc], ["Automation (group)"])
        self.assertEqual(finder.summaries(999), [])

    def test_nothing_is_fetched_when_switched_off(self) -> None:
        self.media.enabled = False
        finder, _search = self.finder()
        self.assertEqual(finder.summaries(1, self.alpha), [])
        self.assertEqual(self.media.titles, [])
        self.settings.fetch_media = False
        plain = Finder(self.catalogue, self.library, self.settings)
        self.assertIsNone(plain.media_file(MediaItem("menu", "https://pics.invalid/x.png", "test")))
        self.assertFalse(plain.media_cache().enabled)
        self.settings.fetch_media = True
        self.settings.online_enabled = False
        self.assertFalse(plain.media_cache().enabled)

    def test_media_file_uses_the_cache(self) -> None:
        finder, _search = self.finder()
        item = MediaItem("menu", "https://pics.invalid/menu.png", "test")
        self.assertEqual(finder.media_file(item), Path("/cache/picture.png"))
        self.assertEqual(self.media.fetched, [item])

    # Cleaning and the brainfile ------------------------------------------------------

    def test_clean_file_files_an_archive_member_like_a_download(self) -> None:
        finder, _search = self.finder()
        library = mock.Mock()
        finder.library = library
        local = LocalFile(path="/nas/set.zip", member="four.st", disk_id=4)
        finder.clean_file(local)
        library.clean_file.assert_called_once_with(
            local, download_folder=self.settings.download_folder, folders=("Games", "Crew")
        )


class RealCatalogueTests(VirusEnvironment, unittest.TestCase):
    """The page search, store and Finder together, on a catalogue the builder wrote."""

    def setUp(self) -> None:
        folder = self.use_environment()
        registry = SeriesRegistry(
            {"automation": SeriesDef("automation", "Automation", "atari-st", "menu", aliases=["a"])}
        )
        records = [
            DiskRecord(
                "tosec",
                "atari-st",
                "menu",
                "automation",
                250,
                contents=[ContentRecord("Necron"), ContentRecord("Tetris")],
                images=[
                    ImageRecordIn("Automation 250 [v Ghost].st", "st", md5="a", virus="Ghost"),
                ],
                locations=[
                    LocationRecord(
                        "host",
                        "https://host.invalid/250.st",
                        image_name="Automation 250 [v Ghost].st",
                    )
                ],
                media=[MediaRecordIn("menu", "https://pics.invalid/250.png")],
                trivia=[
                    TriviaRecordIn("fact", "A famous menu."),
                    TriviaRecordIn("wikipedia", "Necron (video game)", content_title="Necron"),
                ],
            ),
            DiskRecord(
                "tosec",
                "atari-st",
                "menu",
                "automation",
                251,
                contents=[ContentRecord("Rick Dangerous")],
                images=[
                    ImageRecordIn("Automation 251 [v Ghost].st", "st", md5="b", virus="Ghost"),
                    ImageRecordIn("Automation 251 [a].st", "st", md5="c"),
                ],
            ),
        ]
        batch = SourceBatch(
            SourceInfo("tosec", "TOSEC", "https://tosec.invalid"),
            records,
            crews=[
                CrewRecord(
                    "Automation",
                    "tosec",
                    notes="A crew.",
                    wikipedia="Automation (group)",
                    platforms={"atari-st": 2},
                ),
                CrewRecord("Automation", "zoo", notes="An Amiga group.", platforms={"amiga": 9}),
            ],
        )
        path = folder / "catalogue.sqlite"
        connection = sqlite3.connect(path)
        write_catalogue(
            connection,
            merge_records([batch], registry),
            meta={"built_at": "2026-09-12T00:00:00+00:00"},
            groups=GroupRegistry({}, {}),
        )
        connection.commit()
        connection.close()
        self.catalogue = Catalogue.open(path)
        self.addCleanup(self.catalogue.close)
        self.db = UserDatabase.open(folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.library = Library(self.db, self.catalogue)
        self.media = FakeMedia()
        self.finder = Finder(self.catalogue, self.library, Settings(), media=self.media)

    def test_titles_discs_detail_and_flags(self) -> None:
        titles = self.finder.search_page(Query(text="necron"))
        self.assertEqual(titles.total, 1)
        row = titles.rows[0]
        self.assertEqual(
            (row.title, row.disk.label, row.virus), ("Necron", "Automation 250", "Ghost")
        )
        self.assertEqual(row.availability, Availability.ONLINE)
        discs = self.finder.search_page(Query(text="automation", mode=ResultMode.DISCS))
        self.assertEqual(
            {r.disk.label: r.virus for r in discs.rows},
            {"Automation 250": "Ghost", "Automation 251": ""},
        )
        detail = self.finder.detail(row.disk.id)
        self.assertEqual([item.url for item in detail.media], ["https://pics.invalid/250.png"])
        self.assertEqual([item.text for item in detail.trivia], ["A famous menu."])
        self.assertEqual(
            (detail.crew.notes, detail.crew.wikipedia), ("A crew.", "Automation (group)")
        )
        self.assertEqual((detail.virus.status, detail.virus.name), (VirusStatus.FLAGGED, "Ghost"))
        summaries = self.finder.summaries(row.disk.id, row.content_id)
        self.assertEqual(
            [item.title for item in summaries], ["Necron (video game)", "Automation (group)"]
        )
        other = next(r for r in discs.rows if r.disk.label == "Automation 251")
        self.assertEqual(
            [record.name for record in self.finder.clean_alternates(other.disk.id)],
            ["Automation 251 [a].st"],
        )
        self.assertTrue(self.finder.facets().crews)


if __name__ == "__main__":
    unittest.main()
