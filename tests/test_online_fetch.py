from __future__ import annotations

import functools
import gzip
import hashlib
import io
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from piratefinder.jobs.session import download_for_item
from piratefinder.models import ImageRecord, ImageSource, Location, Platform, QueueItem
from piratefinder.online.fetch import FetchError, fetch_location, sanitise_name
from piratefinder.online.http import Downloader, HostThrottle
from piratefinder.settings import Settings
from tests.test_library_helpers import (
    QuietHandler,
    hashes,
    make_adf,
    make_msa,
    make_st_image,
    serve,
)


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-fetch-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.site = self.folder / "site"
        self.site.mkdir()
        self.downloads = self.folder / "downloads"
        self.cache = self.folder / "cache"
        context = serve(functools.partial(QuietHandler, directory=str(self.site)))
        self.base = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.downloader = Downloader(throttle=HostThrottle(0.0), sleep=lambda _s: None, retries=1)
        self.raw = make_st_image("fetch me")

    def publish(self, name: str, data: bytes) -> str:
        (self.site / name).write_bytes(data)
        return f"{self.base}/{name}"

    def fetch(self, location: Location, image: ImageRecord | None = None, **options):
        options.setdefault("folders", ("Games", "Crew"))
        options.setdefault("platform", Platform.ATARI_ST)
        return fetch_location(
            location,
            image,
            download_folder=self.downloads,
            downloader=self.downloader,
            cache_dir=self.cache,
            **options,
        )

    def location(self, url: str, **values: object) -> Location:
        values.setdefault("id", 1)
        return Location(disk_id=1, provider="test", url=url, **values)  # type: ignore[arg-type]

    def assert_cache_empty(self) -> None:
        leftovers = list((self.cache / "downloads").glob("*")) if self.cache.exists() else []
        self.assertEqual(leftovers, [])

    def test_zip_member_checked_against_raw_md5(self) -> None:
        url = self.publish("set.zip", zip_bytes({"Crew 1.st": self.raw, "info.txt": b"x"}))
        image = ImageRecord(id=5, disk_id=1, name="Crew 1 (1990)(Crew).st", format="st")
        notes: list[str] = []
        saved = self.fetch(
            self.location(
                url, container="zip", hash_kind="md5", hash_value=hashes(self.raw)["md5"].upper()
            ),
            image,
            notes=notes,
        )
        self.assertEqual(
            saved, self.downloads / "Atari ST" / "Games" / "Crew" / "Crew 1 (1990)(Crew).st"
        )
        self.assertEqual(saved.read_bytes(), self.raw)
        self.assertEqual(notes, [])
        self.assert_cache_empty()

    def test_msa_checked_against_the_raw_hash_of_a_tosec_st(self) -> None:
        url = self.publish("crew1.msa", make_msa(self.raw))
        image = ImageRecord(id=5, disk_id=1, name="Crew 1 (1990)(Crew).st", format="st")
        saved = self.fetch(
            self.location(url, hash_kind="sha1", hash_value=hashes(self.raw)["sha1"]), image
        )
        self.assertEqual(saved.name, "Crew 1 (1990)(Crew).msa")

    def test_sha512_is_of_the_file_as_stored(self) -> None:
        msa = make_msa(self.raw)
        url = self.publish("crew1.msa", msa)
        saved = self.fetch(
            self.location(url, hash_kind="sha512", hash_value=hashlib.sha512(msa).hexdigest())
        )
        self.assertEqual(saved.read_bytes(), msa)
        with self.assertRaises(FetchError):
            self.fetch(
                self.location(
                    url, hash_kind="sha512", hash_value=hashlib.sha512(self.raw).hexdigest()
                ),
                folders=("Games", "Other"),
            )

    def test_mismatch_deletes_everything(self) -> None:
        url = self.publish("bad.st", make_st_image("something else"))
        with self.assertRaises(FetchError) as caught:
            self.fetch(self.location(url, hash_kind="crc32", hash_value=hashes(self.raw)["crc32"]))
        self.assertIn("does not match the catalogue CRC32 checksum", str(caught.exception))
        self.assertFalse(self.downloads.exists())
        self.assert_cache_empty()

    def test_image_record_hashes_are_used_when_the_location_has_none(self) -> None:
        url = self.publish("plain.st", self.raw)
        good = ImageRecord(
            id=5, disk_id=1, name="Plain.st", format="st", md5=hashes(self.raw)["md5"]
        )
        notes: list[str] = []
        self.fetch(self.location(url), good, notes=notes)
        self.assertEqual(notes, [])
        bad = ImageRecord(id=6, disk_id=1, name="Plain.st", format="st", md5="0" * 32)
        with self.assertRaises(FetchError):
            self.fetch(self.location(url), bad, folders=("Games", "Elsewhere"))
        self.assertFalse((self.downloads / "Atari ST" / "Games" / "Elsewhere").exists())

    def test_without_any_hash_the_image_is_kept_with_a_note(self) -> None:
        url = self.publish("nohash.st", self.raw)
        notes: list[str] = []
        saved = self.fetch(self.location(url), None, folders=(), notes=notes)
        # Without a type and crew the download still gets both levels.
        self.assertEqual(
            saved, self.downloads / "Atari ST" / "Games" / "Unknown crew" / "nohash.st"
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("not checked", notes[0])

    def dump(self, image_id: int, name: str, **values: object) -> ImageRecord:
        return ImageRecord(id=image_id, disk_id=1, name=name, format="st", **values)  # type: ignore[arg-type]

    def test_a_download_without_a_checksum_is_checked_against_every_dump_of_its_disc(
        self,
    ) -> None:
        # A D-Bug MSA with no hash of its own is the TOSEC ST of its disc, decoded.
        msa = make_msa(self.raw)
        url = self.publish("dbug100.msa", msa)
        other = self.dump(5, "D-Bug 100 (1993)(D-Bug)[a].st", md5="0" * 32)
        match = self.dump(6, "D-Bug 100 (1993)(D-Bug).st", md5=hashes(self.raw)["md5"], rank=1)
        notes: list[str] = []
        saved = self.fetch(self.location(url), None, dumps=(other, match), notes=notes)
        # The dump it matched names the file, in the downloaded format.
        self.assertEqual(saved.name, "D-Bug 100 (1993)(D-Bug).msa")
        self.assertEqual(saved.read_bytes(), msa)
        self.assertEqual(
            notes,
            [
                "The download has no checksum of its own; it matched the catalogue dump "
                "D-Bug 100 (1993)(D-Bug).st."
            ],
        )
        self.assert_cache_empty()

    def test_every_hash_the_library_compares_accepts_a_download(self) -> None:
        msa = make_msa(self.raw)
        url = self.publish("pov1.msa", msa)
        by_file = self.dump(5, "POV 1.msa", sha512=hashlib.sha512(msa).hexdigest())
        saved = self.fetch(self.location(url), None, dumps=(by_file,), folders=("Games", "A"))
        self.assertEqual(saved.name, "POV 1.msa")
        by_sha1 = self.dump(6, "POV 1.st", sha1=hashes(self.raw)["sha1"].upper())
        self.fetch(self.location(url), None, dumps=(by_sha1,), folders=("Games", "B"))
        adf = make_adf("pack disk")
        adf_url = self.publish("pack.adf", adf)
        by_crc = self.dump(7, "Pack 1.adf", crc32=hashes(adf)["crc32"], size=len(adf))
        saved = self.fetch(self.location(adf_url), None, dumps=(by_crc,), platform=Platform.AMIGA)
        self.assertEqual(saved.name, "Pack 1.adf")
        # A CRC32 alone, without the size, is not enough to call it checked.
        crc_only = replace(by_crc, size=None)
        notes: list[str] = []
        self.fetch(
            self.location(adf_url),
            None,
            dumps=(crc_only,),
            platform=Platform.AMIGA,
            folders=("Games", "C"),
            notes=notes,
        )
        self.assertIn("not checked", notes[0])

    def test_a_download_that_matches_no_dump_of_its_disc_is_deleted(self) -> None:
        url = self.publish("wrong.msa", make_msa(make_st_image("another disc")))
        dumps = (
            self.dump(5, "Crew 1.st", md5=hashes(self.raw)["md5"]),
            self.dump(6, "Crew 1 [a].st", sha1=hashes(self.raw)["sha1"]),
        )
        with self.assertRaises(FetchError) as caught:
            self.fetch(self.location(url), None, dumps=dumps)
        self.assertIn(
            "does not match the checksum of any dump the catalogue lists for this disc",
            str(caught.exception),
        )
        self.assertFalse(self.downloads.exists())
        self.assert_cache_empty()

    def test_a_dump_without_hashes_falls_back_to_the_other_dumps(self) -> None:
        url = self.publish("tied.st", self.raw)
        tied = self.dump(5, "Tied.st")
        known = self.dump(6, "Known.st", md5=hashes(self.raw)["md5"])
        notes: list[str] = []
        saved = self.fetch(self.location(url, image_id=5), tied, dumps=(tied, known), notes=notes)
        self.assertEqual(saved.name, "Tied.st")
        self.assertIn("Known.st", notes[0])

    def test_a_disc_with_no_known_hash_keeps_the_download_unchecked(self) -> None:
        url = self.publish("nohash.msa", make_msa(self.raw))
        notes: list[str] = []
        saved = self.fetch(
            self.location(url), None, dumps=(self.dump(5, "Crew 1.st"),), notes=notes
        )
        self.assertTrue(saved.exists())
        self.assertEqual(
            notes,
            ["No checksum is known for any dump of this disc, so the download was not checked."],
        )

    def test_download_only_tries_the_next_source_when_a_download_matches_no_dump(self) -> None:
        dumps = (self.dump(5, "Crew 1 (1990)(Crew).st", md5=hashes(self.raw)["md5"]),)
        wrong = self.publish("wrong.msa", make_msa(make_st_image("another disc")))
        right = self.publish("right.msa", make_msa(self.raw))
        sources = [
            ImageSource(label=name, location=self.location(url, id=number), dumps=dumps)
            for number, (name, url) in enumerate((("first", wrong), ("second", right)), start=1)
        ]

        class Finder:
            def sources_for(self, item: QueueItem) -> list[ImageSource]:
                return sources

            def archive_folders(self, disk_id: int) -> tuple[str, str]:
                return ("Games", "Crew")

        class Library:
            def __init__(self) -> None:
                self.added: list[Path] = []

            def add_file(self, path: Path) -> list:
                self.added.append(path)
                return []

        library = Library()
        notes: list[str] = []
        item = QueueItem(id="q", label="Crew 1", platform=Platform.ATARI_ST, disk_id=1)
        saved = download_for_item(
            Finder(),
            library,
            Settings(download_folder=str(self.downloads)),
            item,
            fetcher=functools.partial(fetch_location, cache_dir=self.cache),
            downloader=self.downloader,
            notes=notes,
        )
        self.assertEqual(saved.name, "Crew 1 (1990)(Crew).msa")
        self.assertEqual(library.added, [saved])
        self.assertEqual(len(notes), 1)
        self.assertIn("matched the catalogue dump Crew 1 (1990)(Crew).st", notes[0])
        self.assertEqual(
            [path.name for path in saved.parent.iterdir()], ["Crew 1 (1990)(Crew).msa"]
        )
        self.assert_cache_empty()

    def test_never_overwrites_a_different_file(self) -> None:
        url = self.publish("disk.st", self.raw)
        target = self.downloads / "Atari ST" / "Games" / "Crew" / "disk.st"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"the user's own file")
        saved = self.fetch(self.location(url))
        self.assertEqual(saved.name, "disk (2).st")
        self.assertEqual(target.read_bytes(), b"the user's own file")
        notes: list[str] = []
        again = self.fetch(self.location(url), notes=notes)
        self.assertEqual(again, saved)
        self.assertTrue(any("identical copy" in note for note in notes))
        self.assertEqual(
            sorted(p.name for p in target.parent.iterdir()), ["disk (2).st", "disk.st"]
        )

    def test_member_choice(self) -> None:
        other = make_st_image("other")
        url = self.publish("two.zip", zip_bytes({"A.st": self.raw, "B.st": other}))
        saved = self.fetch(self.location(url, container="zip", member="B.st"))
        self.assertEqual(saved.read_bytes(), other)
        with self.assertRaises(FetchError) as caught:
            self.fetch(self.location(url, container="zip"))
        self.assertIn("2 disk images", str(caught.exception))
        with self.assertRaises(FetchError):
            self.fetch(self.location(url, container="zip", member="C.st"))

    def test_nested_zip_and_member_by_base_name(self) -> None:
        inner = zip_bytes({"Crew 1.st": self.raw})
        url = self.publish("outer.zip", zip_bytes({"folder/crew1.zip": inner}))
        saved = self.fetch(self.location(url, container="zip", member="Crew 1.st"))
        self.assertEqual(saved.read_bytes(), self.raw)
        self.assertEqual(saved.name, "Crew 1.st")

    def test_gzip_and_amiga_folder(self) -> None:
        adf = make_adf("amiga fetch")
        url = self.publish("Disk.adf.gz", gzip.compress(adf))
        saved = self.fetch(
            self.location(url, hash_kind="md5", hash_value=hashes(adf)["md5"]),
            platform=Platform.AMIGA,
            folders=("Demos", "Some/Crew"),
        )
        # A crew name never makes a folder level of its own.
        self.assertEqual(saved, self.downloads / "Amiga" / "Demos" / "Some_Crew" / "Disk.adf")
        self.assertEqual(saved.read_bytes(), adf)

    def test_zip_detected_without_a_suffix(self) -> None:
        url = self.publish("download", zip_bytes({"Crew 1.st": self.raw}))
        saved = self.fetch(self.location(url))
        self.assertEqual(saved.name, "Crew 1.st")

    def test_missing_file(self) -> None:
        with self.assertRaises(FetchError) as caught:
            self.fetch(self.location(f"{self.base}/absent.st"))
        self.assertIn("404", str(caught.exception))

    def test_sanitise_name(self) -> None:
        self.assertEqual(sanitise_name('D-Bug/Menu: "1"?'), "D-Bug_Menu_ _1__")
        self.assertEqual(sanitise_name("..hidden. "), "hidden")
        self.assertEqual(sanitise_name(""), "download")
        long = sanitise_name("x" * 300 + ".st")
        self.assertTrue(long.endswith(".st"))
        self.assertLessEqual(len(long.encode()), 200)


if __name__ == "__main__":
    unittest.main()
