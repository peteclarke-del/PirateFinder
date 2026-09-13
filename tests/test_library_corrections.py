from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from piratefinder.catalogue.search import CataloguePage, CatalogueRow
from piratefinder.catalogue.store import Catalogue
from piratefinder.finder import Finder
from piratefinder.library.corrections import (
    Correction,
    CorrectionError,
    apply_contents,
    apply_disk,
    correction_from_form,
    parse_date,
)
from piratefinder.library.library import Library
from piratefinder.library.userdb import UserDatabase
from piratefinder.models import Content, ContentKind, Disk, DiskKind, Platform, Query, ResultMode
from piratefinder.settings import Settings
from tests.test_library_helpers import CatalogueBuilder, make_adf, make_st_image


class DateTests(unittest.TestCase):
    def test_the_three_forms_and_empty(self) -> None:
        self.assertEqual(parse_date("1990"), (1990, None, None))
        self.assertEqual(parse_date(" 1990-06 "), (1990, 6, None))
        self.assertEqual(parse_date("1990-06-21"), (1990, 6, 21))
        self.assertEqual(parse_date(""), (None, None, None))

    def test_anything_else_is_refused_with_a_sentence(self) -> None:
        for text in ("90", "1990-6", "1990/06", "June 1990", "1990-06-21-1", "19900"):
            with self.subTest(text=text), self.assertRaises(CorrectionError) as caught:
                parse_date(text)
            self.assertIn("YYYY, YYYY-MM or YYYY-MM-DD", str(caught.exception))
        for text in ("1990-13", "1990-02-30", "0000", "1990-00", "1990-01-00"):
            with self.subTest(text=text), self.assertRaises(CorrectionError) as caught:
                parse_date(text)
            self.assertEqual(str(caught.exception), f"{text} is not a date in the calendar.")


class FormTests(unittest.TestCase):
    disk = Disk(1, "Automation 250", Platform.ATARI_ST, DiskKind.MENU, crew="Automation", year=1990)
    contents = (
        Content(1, "Necron", ContentKind.GAME, 1, id=11),
        Content(1, "Xenon", ContentKind.GAME, 2, id=12),
        Content(1, "Necron", ContentKind.DOC, 3, id=13),
    )

    def test_only_what_differs_from_the_catalogue_is_a_correction(self) -> None:
        form = {"label": "Automation 250", "crew": " The  Automation ", "date": "1990"}
        correction = correction_from_form(self.disk, self.contents, form, {11: "Necron"})
        self.assertEqual(correction, Correction({"crew": "The Automation"}, {}))
        self.assertFalse(correction_from_form(self.disk, self.contents, {"date": "1990"}))

    def test_a_date_sets_year_month_and_day(self) -> None:
        correction = correction_from_form(self.disk, self.contents, {"date": "1991-02-03"})
        disk = apply_disk(self.disk, correction)
        self.assertEqual((disk.date, disk.year, disk.month, disk.day), ("1991-02-03", 1991, 2, 3))
        cleared = apply_disk(self.disk, correction_from_form(self.disk, (), {"date": ""}))
        self.assertIsNone(cleared.year)
        with self.assertRaises(CorrectionError):
            correction_from_form(self.disk, self.contents, {"date": "1991-2"})

    def test_a_label_and_a_title_need_a_name(self) -> None:
        with self.assertRaises(CorrectionError):
            correction_from_form(self.disk, self.contents, {"label": "  "})
        with self.assertRaises(CorrectionError):
            correction_from_form(self.disk, self.contents, {}, {12: ""})

    def test_titles_of_the_same_name_are_told_apart_by_their_place(self) -> None:
        correction = correction_from_form(self.disk, self.contents, {}, {13: "Necron Docs"})
        self.assertEqual(correction.titles, {("Necron", 2): "Necron Docs"})
        titles = [content.title for content in apply_contents(self.contents, correction)]
        self.assertEqual(titles, ["Necron", "Xenon", "Necron Docs"])


def build(
    path: Path,
    built_at: str,
    *,
    shift: int = 0,
    drop_pack: bool = False,
    pack_first: bool = False,
) -> Path:
    """A catalogue with a numbered disc and a pack known only by its dump.

    ``shift`` puts that many other discs (and titles) first, so every id moves,
    as it does when a new catalogue build gains discs. ``pack_first`` swaps the
    ids of the menu disc and the pack.
    """
    builder = CatalogueBuilder(path, built_at=built_at)
    for number in range(shift):
        builder.disk(1 + number, f"Other {number}", contents=[f"Other {number}"])
    if pack_first:
        builder.disk(1, "Pack", platform=Platform.AMIGA, kind=DiskKind.PACK)
        builder.image(20, 1, "Pack.adf", data=make_adf("pack"))
        shift, drop_pack = 1, True
    menu = 1 + shift
    builder.disk(
        menu,
        "Automation 250",
        series=("automation", "Automation"),
        number=250,
        contents=["Necron", "Xenon", "Necron"],
        crew="Automation",
    )
    builder.connection.execute(
        "UPDATE disks SET publisher = 'Menu Publisher', year = 1990 WHERE id = ?", (menu,)
    )
    builder.image(10 + shift, menu, "Automation 250.st", data=make_st_image("a250"))
    if not drop_pack:
        builder.disk(2 + shift, "Pack", platform=Platform.AMIGA, kind=DiskKind.PACK)
        builder.image(20 + shift, 2 + shift, "Pack.adf", data=make_adf("pack"))
    builder.disk(
        50, "Automation 251", series=("automation", "Automation"), number=251, contents=["Necron"]
    )
    return builder.close()


class FinderCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-corrections-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.db = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.catalogue = self.open(build(self.folder / "one.sqlite", "2026-09-01"))
        self.library = Library(self.db, self.catalogue)
        self.settings = Settings(download_folder=str(self.folder / "downloads"))
        self.page = CataloguePage([], 0)
        self.searched: list = []  # the overrides each search was given

        def search(_catalogue, _query, **arguments):
            self.searched.append(arguments["overrides"])
            return self.page

        self.finder = Finder(self.catalogue, self.library, self.settings, search_page=search)

    def open(self, path: Path) -> Catalogue:
        catalogue = Catalogue.open(path)
        self.addCleanup(catalogue.close)
        return catalogue

    def edit_menu(self, disk_id: int = 1) -> None:
        contents = self.catalogue.contents(disk_id)
        self.finder.save_details(
            disk_id,
            {
                "label": "Automation 250 A",
                "crew": "The Automation",
                "date": "1990-06-21",
                "publisher": "Menu Publisher",
                "notes": "Side B is blank.",
            },
            {contents[2].id: "Necron Docs"},
        )

    def test_the_details_pane_shows_corrections_and_marks_them(self) -> None:
        self.edit_menu()
        detail = self.finder.detail(1)
        disk = detail.disk
        self.assertEqual(
            (disk.label, disk.crew, disk.year, disk.month, disk.day, disk.notes),
            ("Automation 250 A", "The Automation", 1990, 6, 21, "Side B is blank."),
        )
        self.assertEqual(disk.publisher, "Menu Publisher")
        self.assertEqual(detail.edited, ("label", "crew", "date", "notes"))
        self.assertEqual(detail.original.label, "Automation 250")
        self.assertEqual([c.title for c in detail.contents], ["Necron", "Xenon", "Necron Docs"])
        self.assertEqual(detail.edited_titles, ((detail.contents[2].id, "Necron"),))
        untouched = self.finder.detail(2)
        self.assertEqual((untouched.edited, untouched.original), ((), None))

    def test_the_search_is_given_the_corrections(self) -> None:
        self.edit_menu()
        self.finder.save_details(2, {"label": "Pack One"})
        self.finder.search_page(Query(text="necron"))
        overrides = self.searched[-1]
        necron_docs = self.catalogue.contents(1)[2].id
        self.assertEqual(sorted(overrides.disks), [1, 2])
        corrected = overrides.disks[1]
        self.assertEqual(
            (corrected.label, corrected.crew, corrected.year, corrected.month),
            ("Automation 250 A", "The Automation", 1990, 6),
        )
        self.assertEqual(overrides.disks[2].label, "Pack One")
        self.assertEqual(overrides.titles, {necron_docs: (1, "Necron Docs")})
        # The same object serves every search until a correction changes.
        self.finder.search_page(Query(text="xenon"))
        self.assertIs(self.searched[-1], overrides)
        self.assertIs(self.finder.search_overrides(), overrides)
        self.finder.revert_details(2)
        self.assertEqual(sorted(self.finder.search_overrides().disks), [1])

    def test_result_rows_show_corrections(self) -> None:
        self.edit_menu()
        necron_docs = self.catalogue.contents(1)[2].id
        # The search gives corrected titles (see test_catalogue_search); the
        # finder shows the corrected disc with them.
        self.page = CataloguePage(
            [
                CatalogueRow(1, necron_docs, "Necron Docs", ContentKind.DOC, 1.0, ("Necron Docs",)),
                CatalogueRow(2, None, "Pack", None, 0.5),
            ],
            2,
        )
        rows = self.finder.search_page(Query(text="necron")).rows
        self.assertEqual([row.title for row in rows], ["Necron Docs", "Pack"])
        self.assertEqual(rows[0].disk.label, "Automation 250 A")
        self.assertEqual(rows[0].matched, ("Necron Docs",))
        self.page = CataloguePage([CatalogueRow(1, None, "", None, 1.0, ("Necron",))], 1)
        (disc,) = self.finder.search_page(Query(text="necron", mode=ResultMode.DISCS)).rows
        self.assertEqual(disc.summary, "Necron, Xenon, Necron Docs")
        self.assertEqual(disc.disk.crew, "The Automation")

    def test_a_title_renamed_on_one_disc_keeps_its_name_on_another(self) -> None:
        self.edit_menu()
        self.page = CataloguePage(
            [
                CatalogueRow(1, None, "", None, 1.0, ("Necron", "Necron Docs")),
                CatalogueRow(50, None, "", None, 1.0, ("Necron",)),
            ],
            2,
        )
        rows = self.finder.search_page(Query(text="necron", mode=ResultMode.DISCS)).rows
        self.assertEqual([row.matched for row in rows], [("Necron", "Necron Docs"), ("Necron",)])
        self.assertEqual([row.summary for row in rows], ["Necron, Xenon, Necron Docs", "Necron"])
        self.assertEqual(
            self.searched[-1].titles, {self.catalogue.contents(1)[2].id: (1, "Necron Docs")}
        )

    def test_facets_count_corrected_crews(self) -> None:
        self.edit_menu()
        crews = dict(Finder(self.catalogue, self.library, self.settings).facets().crews)
        self.assertEqual(crews.get("The Automation"), 1)

    def test_downloads_are_filed_under_the_corrected_crew(self) -> None:
        self.assertEqual(self.finder.archive_folders(1), ("Games", "Automation"))
        self.edit_menu()
        self.assertEqual(self.finder.archive_folders(1), ("Games", "The Automation"))

    def test_revert_and_saving_the_catalogue_values_forget_the_correction(self) -> None:
        self.edit_menu()
        self.finder.revert_details(1)
        detail = self.finder.detail(1)
        self.assertEqual(
            (detail.disk.label, detail.edited, detail.edited_titles), ("Automation 250", (), ())
        )
        self.edit_menu()
        self.finder.save_details(
            1,
            {"label": "Automation 250", "crew": "Automation", "date": "1990", "notes": ""},
            {self.catalogue.contents(1)[2].id: "Necron"},
        )
        self.assertEqual(self.finder.detail(1).edited, ())
        self.assertEqual(self.db.corrected_discs(), [])

    def test_a_bad_date_is_not_stored(self) -> None:
        with self.assertRaises(CorrectionError):
            self.finder.save_details(1, {"label": "Changed", "date": "21/06/1990"})
        self.assertEqual(self.finder.detail(1).edited, ())

    def test_corrections_follow_their_discs_into_a_new_catalogue(self) -> None:
        self.edit_menu()
        self.finder.save_details(2, {"title": "Pack (Corrected)"})
        # A newer build puts two discs first, so every disc and title id moves.
        newer = self.open(build(self.folder / "two.sqlite", "2026-09-08", shift=2))
        self.finder.set_catalogue(newer)
        self.assertEqual(self.finder.detail(1).edited, (), "disc 1 is another disc now")
        moved = self.finder.detail(3)
        self.assertEqual(moved.disk.label, "Automation 250 A")
        self.assertEqual([c.title for c in moved.contents], ["Necron", "Xenon", "Necron Docs"])
        pack = self.finder.detail(4)
        self.assertEqual((pack.disk.label, pack.disk.title), ("Pack", "Pack (Corrected)"))
        # A build without the pack keeps its correction for a later build that has it.
        without = self.open(build(self.folder / "three.sqlite", "2026-09-15", drop_pack=True))
        self.finder.set_catalogue(without)
        self.assertEqual(self.finder.detail(1).disk.label, "Automation 250 A")
        self.assertEqual(len(self.db.corrected_discs()), 2)
        again = self.open(build(self.folder / "four.sqlite", "2026-09-22", shift=1))
        self.finder.set_catalogue(again)
        self.assertEqual(self.finder.detail(3).disk.title, "Pack (Corrected)")
        self.assertEqual(self.finder.detail(2).disk.label, "Automation 250 A")

    def test_a_reader_still_on_the_old_catalogue_never_gets_another_discs_corrections(
        self,
    ) -> None:
        self.finder.save_details(2, {"title": "Pack (Corrected)"})
        stale = Finder(self.catalogue, self.library, self.settings)
        self.assertEqual(stale.detail(2).disk.title, "Pack (Corrected)")
        # The newer build numbers the pack 1, which is the menu disc in the old one.
        newer = self.open(build(self.folder / "two.sqlite", "2026-09-08", pack_first=True))
        self.finder.set_catalogue(newer)
        self.assertEqual(self.finder.detail(1).disk.title, "Pack (Corrected)")
        self.assertEqual(stale.detail(1).edited, (), "disc 1 of the old catalogue is the menu")


if __name__ == "__main__":
    unittest.main()
