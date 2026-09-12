"""Where downloads are filed: <platform>/<type>/<crew>/<file>."""

from __future__ import annotations

import unittest

from piratefinder.archive_layout import (
    APPLICATIONS,
    DEMOS,
    GAMES,
    MUSIC,
    UNKNOWN_CREW,
    archive_crew,
    archive_folders,
    archive_type,
    platform_folder,
)
from piratefinder.models import Content, ContentKind, Disk, DiskKind, Platform


def disk(kind: DiskKind = DiskKind.MENU, **fields) -> Disk:
    return Disk(1, "Disk 1", fields.pop("platform", Platform.ATARI_ST), kind, **fields)


def contents(*kinds: ContentKind) -> list[Content]:
    return [Content(1, f"Title {n}", kind, n) for n, kind in enumerate(kinds)]


class PlatformTests(unittest.TestCase):
    def test_platform_folders(self) -> None:
        self.assertEqual(platform_folder(Platform.ATARI_ST), "Atari ST")
        self.assertEqual(platform_folder("amiga"), "Amiga")
        self.assertEqual(platform_folder(None), "Other")


class TypeTests(unittest.TestCase):
    def test_the_main_programs_decide_the_type(self) -> None:
        G, U, D, M = ContentKind.GAME, ContentKind.UTILITY, ContentKind.DEMO, ContentKind.MUSIC
        self.assertEqual(archive_type(disk(), contents(G, G, U)), GAMES)
        self.assertEqual(archive_type(disk(), contents(U, U, G)), APPLICATIONS)
        self.assertEqual(archive_type(disk(DiskKind.PACK), contents(D, D, M)), DEMOS)
        self.assertEqual(archive_type(disk(DiskKind.PACK), contents(M)), MUSIC)

    def test_intros_and_documents_do_not_outvote_the_games_on_a_menu(self) -> None:
        menu = contents(ContentKind.GAME, ContentKind.INTRO, ContentKind.INTRO, ContentKind.DOC)
        self.assertEqual(archive_type(disk(), menu), GAMES)
        self.assertEqual(archive_type(disk(), contents(ContentKind.INTRO)), DEMOS)

    def test_a_disk_without_contents_is_filed_by_its_kind(self) -> None:
        self.assertEqual(archive_type(disk(DiskKind.MENU), []), GAMES)
        self.assertEqual(archive_type(disk(DiskKind.PACK), []), DEMOS)
        self.assertEqual(archive_type(disk(DiskKind.SINGLE), []), GAMES)


class CrewTests(unittest.TestCase):
    def test_a_series_belongs_to_its_crew(self) -> None:
        compact = disk(series_id="skid-row-compact", series_name="Skid Row Compact")
        self.assertEqual(archive_crew(compact, "Skid Row"), "Skid Row")
        self.assertEqual(archive_crew(compact, ""), "Skid Row Compact")

    def test_a_single_disk_belongs_to_its_cracker_then_its_publisher(self) -> None:
        cracked = disk(DiskKind.SINGLE, cracker="Quartex", publisher="Psygnosis")
        self.assertEqual(archive_crew(cracked), "Quartex")
        original = disk(DiskKind.SINGLE, publisher="Psygnosis")
        self.assertEqual(archive_crew(original), "Psygnosis")
        self.assertEqual(archive_crew(disk(DiskKind.SINGLE)), UNKNOWN_CREW)

    def test_folders(self) -> None:
        automation = disk(series_id="automation", series_name="Automation")
        self.assertEqual(
            archive_folders(automation, contents(ContentKind.GAME), "Automation"),
            (GAMES, "Automation"),
        )


if __name__ == "__main__":
    unittest.main()
