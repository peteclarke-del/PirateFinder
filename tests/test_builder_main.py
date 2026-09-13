"""The catalogue builder command: source discovery, selection and output files."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import importlib
import io
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from catalogue_builder.__main__ import discover, main, select

MODULES = {
    "__init__.py": "",
    "good.py": """
        from catalogue_builder.records import DiskRecord, ImageRecordIn, SourceInfo
        INFO = SourceInfo("good", "Good source", "https://good.example", "CC0")
        CONTENT_PRIORITY = 10
        RETRIEVED = "2026-01-02"
        def collect(ctx):
            yield DiskRecord("good", "atari-st", "menu", "automation", 250,
                             images=[ImageRecordIn("a.st", "st", md5="aa")])
    """,
    "plain.py": """
        from catalogue_builder.records import DiskRecord, ImageRecordIn, SourceInfo
        INFO = SourceInfo("plain", "Plain source", "https://plain.example")
        def collect(ctx):
            yield DiskRecord("plain", "amiga", "single", title="Xenon (1988)(Melbourne House)",
                             images=[ImageRecordIn("x.adf", "adf", md5="bb")])
    """,
    "optional.py": """
        from catalogue_builder.records import DiskRecord, SourceInfo
        INFO = SourceInfo("optional", "Optional source", "https://optional.example")
        DEFAULT_ENABLED = False
        CONTENT_PRIORITY = 35
        def collect(ctx):
            yield DiskRecord("optional", "amiga", "pack", title="Some Pack")
    """,
    "failing.py": """
        from catalogue_builder.records import SourceInfo
        INFO = SourceInfo("failing", "Failing source", "https://failing.example")
        def collect(ctx):
            raise ConnectionError("site down")
    """,
    "helpers.py": "VALUE = 1\n",
}


class BuilderCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.name = f"fake_sources_{id(self)}"
        package = root / self.name
        package.mkdir()
        for file_name, source in MODULES.items():
            (package / file_name).write_text(textwrap.dedent(source))
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.package = importlib.import_module(self.name)
        self.output = root / "build" / "catalogue.sqlite"
        self.logged: list[str] = []

    def run_main(self, *arguments: str) -> int:
        argv = ["--output", str(self.output), "--cache", self.folder.name, *arguments]
        return main(argv, log=self.logged.append, package=self.package)

    def test_discovery_and_selection(self) -> None:
        (Path(self.folder.name) / self.name / "unimportable.py").write_text("import missing_x\n")
        found, broken = discover(self.package, log=self.logged.append)
        self.assertEqual([s.info.id for s in found], ["good", "optional", "failing", "plain"])
        self.assertEqual([s.priority for s in found], [10, 35, 90, 90])
        self.assertIn("unimportable", broken)

        def ids(chosen: list) -> list[str]:
            return [source.info.id for source in chosen]

        self.assertEqual(
            ids(select(found, only=set(), skip=set(), extra=set())), ["good", "failing", "plain"]
        )
        self.assertEqual(
            ids(select(found, only=set(), skip={"failing"}, extra={"optional"})),
            ["good", "optional", "plain"],
        )
        self.assertEqual(
            ids(select(found, only={"optional", "plain"}, skip=set(), extra=set())),
            ["optional", "plain"],
        )

    def test_a_failing_source_is_skipped_and_the_catalogue_published(self) -> None:
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(
            any("failing: failed: ConnectionError: site down" in line for line in self.logged)
        )
        self.assertFalse(self.output.with_name("catalogue.sqlite.tmp").exists())
        with sqlite3.connect(self.output) as connection:
            labels = [row[0] for row in connection.execute("SELECT label FROM disks ORDER BY id")]
            meta = dict(connection.execute("SELECT key, value FROM meta"))
            sources = connection.execute(
                "SELECT id, retrieved, records FROM sources ORDER BY id"
            ).fetchall()
        self.assertEqual(labels, ["Automation 250", "Xenon"])  # numbered disks first
        self.assertEqual(meta["source:failing"], "failed: ConnectionError: site down")
        self.assertEqual(meta["source:good"], "ok, 1 records")
        self.assertRegex(meta["built_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(sources[0], ("good", "2026-01-02", 1))
        compressed = self.output.with_name("catalogue.sqlite.gz")
        self.assertEqual(gzip.decompress(compressed.read_bytes()), self.output.read_bytes())
        checksum = compressed.with_name("catalogue.sqlite.gz.sha256").read_text().split()
        self.assertEqual(
            checksum, [hashlib.sha256(compressed.read_bytes()).hexdigest(), "catalogue.sqlite.gz"]
        )

    def test_strict_stops_at_a_failing_source(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main("--strict"), 1)
        self.assertFalse(self.output.exists())

    def test_only_skip_and_with(self) -> None:
        self.assertEqual(self.run_main("--only", "plain,optional"), 0)
        with sqlite3.connect(self.output) as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM sources ORDER BY id")]
        self.assertEqual(ids, ["optional", "plain"])
        self.assertEqual(self.run_main("--skip", "failing", "--with", "optional"), 0)
        with sqlite3.connect(self.output) as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM sources ORDER BY id")]
        self.assertEqual(ids, ["good", "optional", "plain"])
        self.assertEqual(self.run_main("--only", "nothing-by-this-name"), 1)

    def test_list_sources(self) -> None:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(self.run_main("--list-sources"), 0)
        lines = printed.getvalue().splitlines()
        self.assertEqual(
            [line.split()[0] for line in lines], ["good", "optional", "failing", "plain"]
        )
        self.assertIn("off", lines[1])
        self.assertFalse(self.output.exists())

    def test_input_must_name_a_source(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main("--input", "no-equals-sign")


class SourceLicenceTest(unittest.TestCase):
    """Every row of the sources table names a licence or the terms a source is used under."""

    def test_every_source_states_its_licence_or_terms(self) -> None:
        from catalogue_builder import sources
        from catalogue_builder.merge import CREDITED_SOURCES

        found, broken = discover(sources, log=lambda message: None)
        self.assertEqual(broken, {})
        self.assertGreater(len(found), 10)
        for source in found:
            self.assertTrue(source.info.licence.strip(), source.info.id)
        for info in CREDITED_SOURCES.values():
            self.assertTrue(info.licence.strip(), info.id)


if __name__ == "__main__":
    unittest.main()
