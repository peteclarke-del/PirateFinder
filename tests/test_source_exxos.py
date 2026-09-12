"""Tests for the exxos importer: Persistence of Vision gallery pages."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.context import BuildContext
from catalogue_builder.series import SeriesRegistry
from catalogue_builder.sources import exxos

FIXTURES = Path(__file__).parent / "fixtures" / "exxos"
BASE = exxos.FIRST_PAGE.rsplit("/", 1)[0]


class ParseTest(unittest.TestCase):
    def test_cells_and_page_links(self) -> None:
        entries, links = exxos.parse_page(exxos.decode((FIXTURES / "page1.htm").read_bytes()))
        self.assertEqual(links, ["page1.htm", "page2.htm"])
        first = entries[0]
        self.assertEqual(first.heading, "POV_001")
        self.assertEqual(first.lines, ["Micromix I", "Micromix II"])
        self.assertEqual(first.href, "files/001(1989).zip")
        self.assertIn("POV_006", [entry.heading for entry in entries])

    def test_cell_without_link(self) -> None:
        entries, _links = exxos.parse_page(exxos.decode((FIXTURES / "page2.htm").read_bytes()))
        other = entries[-1]
        self.assertEqual(
            (other.heading, other.lines, other.href), ("Other Disk", ["Not a POV disk"], "")
        )

    def test_text_after_a_cell_is_not_content(self) -> None:
        page = "<div><h3>POV_009</h3>Demo One<br></div><p>Back to the index</p>"
        [entry], _links = exxos.parse_page(page)
        self.assertEqual(entry.lines, ["Demo One"])

    def test_intro_lines(self) -> None:
        self.assertEqual(exxos.content("intro : D-Generation").kind, "intro")
        self.assertEqual(exxos.content("intro : D-Generation").title, "D-Generation")
        self.assertEqual(exxos.content("Copier Screen").kind, "demo")

    def test_decode_falls_back_to_windows_latin(self) -> None:
        self.assertEqual(exxos.decode(b"Ol\xe9"), "Ol\xe9")
        self.assertEqual(exxos.decode(b"Ol\xc3\xa9"), "Ol\xe9")


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.messages: list[str] = []
        ctx = BuildContext(
            cache_dir=Path(folder.name),
            series=SeriesRegistry.load(),
            offline=True,
            log=self.messages.append,
        )
        for name in ("page1.htm", "page2.htm"):
            target = ctx.cache_path(f"{BASE}/{name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURES / name, target)
        self.records = {record.number: record for record in exxos.collect(ctx)}

    def test_every_pov_cell_on_every_page(self) -> None:
        self.assertEqual(sorted(self.records), [1, 2, 5, 6, 135, 136, 165])
        self.assertIn(
            "exxos: 7 disks from 2 pages, 1 headings not recognised by any series rule",
            self.messages,
        )

    def test_disk_record(self) -> None:
        record = self.records[1]
        self.assertEqual(record.key, ("persistence-of-vision", 1, "", ""))
        self.assertEqual(
            (record.source, record.platform, record.kind), ("exxos", "atari-st", "pack")
        )
        self.assertEqual((record.title, record.date), ("", "1989"))
        self.assertEqual([c.title for c in record.contents], ["Micromix I", "Micromix II"])
        self.assertEqual({c.kind for c in record.contents}, {"demo"})
        self.assertEqual(record.links, [("exxos", f"{BASE}/page1.htm")])
        [location] = record.locations
        self.assertEqual(location.url, f"{BASE}/files/001(1989).zip")
        self.assertEqual(
            (location.provider, location.container, location.member), ("exxos", "zip", "")
        )
        self.assertEqual((location.priority, location.hash_value), (40, ""))
        self.assertEqual(location.page_url, f"{BASE}/page1.htm")

    def test_version_comes_from_the_zip_name(self) -> None:
        self.assertEqual(self.records[6].key, ("persistence-of-vision", 6, "", "v2"))

    def test_zip_flags_and_encoded_names(self) -> None:
        record = self.records[136]
        self.assertEqual(record.notes, "[STE]")
        self.assertEqual(record.locations[0].url, f"{BASE}/files/136(1993)%5BSTE%5D.zip")
        self.assertEqual(record.links, [("exxos", f"{BASE}/page2.htm")])

    def test_intros_on_a_disk(self) -> None:
        kinds = [(c.title, c.kind) for c in self.records[165].contents]
        self.assertIn(("D-Generation", "intro"), kinds)
        self.assertIn(("Oh-No! More Mario", "demo"), kinds)


if __name__ == "__main__":
    unittest.main()
