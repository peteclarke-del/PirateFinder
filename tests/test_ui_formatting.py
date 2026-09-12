"""Text the interface shows, tested without a display."""

from __future__ import annotations

import unittest

from piratefinder.models import (
    Content,
    ContentKind,
    LocalFile,
    Platform,
    ScanSummary,
    SessionSummary,
    WriteOutcome,
    WriteStatus,
)
from piratefinder.ui import formatting as fmt
from piratefinder.ui.help_content import DATA_CREDITS, EXAMPLE_SEARCHES, HELP_TOPICS


def outcome(status: WriteStatus, summary: str = "") -> WriteOutcome:
    return WriteOutcome(status, summary)


class SummaryMarkupTests(unittest.TestCase):
    def test_matched_titles_are_bold_and_everything_is_escaped(self) -> None:
        markup = fmt.summary_markup("Rick & Co, <Menace>", ["rick & co"])
        self.assertEqual(markup, "<b>Rick &amp; Co</b>, &lt;Menace&gt;")

    def test_every_occurrence_is_bold_and_overlaps_are_not_nested(self) -> None:
        markup = fmt.summary_markup("Oids, Xenon 2, Oids", ["Oids", "Xenon 2", "Xenon"])
        self.assertEqual(markup, "<b>Oids</b>, <b>Xenon 2</b>, <b>Oids</b>")

    def test_a_match_missing_from_a_shortened_summary_is_put_first(self) -> None:
        markup = fmt.summary_markup("Menace and 4 more", ["Speedball <2>"])
        self.assertEqual(markup, "<b>Speedball &lt;2&gt;</b> • Menace and 4 more")

    def test_no_matches_leaves_plain_escaped_text(self) -> None:
        self.assertEqual(fmt.summary_markup("A & B", []), "A &amp; B")


class StickerTests(unittest.TestCase):
    def contents(self, *titles: tuple[str, ContentKind]) -> list[Content]:
        return [Content(1, title, kind, position) for position, (title, kind) in enumerate(titles)]

    def test_programs_in_menu_order_without_documents(self) -> None:
        contents = self.contents(
            ("Necron", ContentKind.GAME),
            ("Docs", ContentKind.DOC),
            ("Boulderdash CK", ContentKind.GAME),
        )
        self.assertEqual(
            fmt.sticker_text("Automation 250", contents), "Automation 250: Necron, Boulderdash CK"
        )

    def test_titles_that_do_not_fit_are_counted(self) -> None:
        contents = self.contents(
            *[(f"Game Number {index}", ContentKind.GAME) for index in range(8)]
        )
        text = fmt.sticker_text("Disk 1", contents, max_chars=48)
        self.assertLessEqual(len(text), 48)
        self.assertTrue(text.startswith("Disk 1: Game Number 0"))
        self.assertRegex(text, r"\+\d more$")

    def test_a_disk_without_contents_is_just_its_label(self) -> None:
        self.assertEqual(fmt.sticker_text("D-Bug 100 B", []), "D-Bug 100 B")


class SessionTextTests(unittest.TestCase):
    def summary(self, *statuses: WriteStatus) -> SessionSummary:
        items = tuple(
            (f"Disk {index}", outcome(status), "") for index, status in enumerate(statuses)
        )
        return SessionSummary("2026-09-12T14:03:00", "2026-09-12T14:20:00", "A", items)

    def test_headline_counts_written_and_verified(self) -> None:
        summary = self.summary(*[WriteStatus.VERIFIED] * 4, WriteStatus.FAILED)
        self.assertEqual(fmt.session_headline(summary), "4 of 5 disks written and verified")
        self.assertEqual(fmt.session_title(summary), "Some Disks Not Written")

    def test_headline_mentions_unverified_flux_writes(self) -> None:
        summary = self.summary(WriteStatus.VERIFIED, WriteStatus.WRITTEN)
        self.assertIn("1 without verification", fmt.session_headline(summary))

    def test_all_written(self) -> None:
        self.assertEqual(fmt.session_title(self.summary(WriteStatus.VERIFIED)), "All Disks Written")

    def test_failed_labels_leave_out_skipped_disks(self) -> None:
        summary = self.summary(WriteStatus.FAILED, WriteStatus.SKIPPED, WriteStatus.NO_DISK)
        self.assertEqual(fmt.failed_labels(summary), ["Disk 0", "Disk 2"])

    def test_report_lists_every_disk(self) -> None:
        report = fmt.report_text(self.summary(WriteStatus.VERIFIED, WriteStatus.FAILED))
        self.assertIn("Disk 0: Written and verified", report)
        self.assertIn("Disk 1: Failed", report)


class PromptTextTests(unittest.TestCase):
    def test_insert_prompt_names_disk_drive_and_warns(self) -> None:
        self.assertEqual(fmt.insert_heading("Automation 250"), "Insert a Disk for Automation 250")
        self.assertEqual(
            fmt.insert_body(2, 5, "A"),
            "Disk 2 of 5, drive A. Everything on the floppy will be overwritten.",
        )

    def test_a_known_reason_becomes_a_sentence(self) -> None:
        body = fmt.insert_body(1, 1, "B", "write-protected")
        self.assertTrue(body.startswith("The disk is write-protected. Slide the tab"))

    def test_a_sentence_from_the_session_is_shown_as_it_is(self) -> None:
        body = fmt.insert_body(1, 1, "0", "There is no disk in the drive. Insert a disk.")
        self.assertTrue(body.startswith("There is no disk in the drive."))


class SmallTextTests(unittest.TestCase):
    def test_plural_and_thousands(self) -> None:
        self.assertEqual(fmt.plural(1, "disk"), "1 disk")
        self.assertEqual(fmt.plural(1234, "image"), "1,234 images")
        self.assertEqual(fmt.plural(2, "copy", "copies"), "2 copies")

    def test_scan_toast(self) -> None:
        summary = ScanSummary(("/a",), 1300, 1234, 1100, 134, 10, 0)
        self.assertEqual(
            fmt.scan_toast(summary), "Found 1,234 images: 1,100 matched the catalogue, 134 did not"
        )

    def test_timestamps(self) -> None:
        self.assertEqual(fmt.format_timestamp("2026-09-01"), "1 Sep 2026")
        self.assertEqual(fmt.format_timestamp("2026-09-12T14:03:00"), "12 Sep 2026, 14:03")
        self.assertEqual(fmt.format_timestamp("not a date"), "not a date")

    def test_provider_names_fall_back_to_title_case(self) -> None:
        self.assertEqual(fmt.provider_name("internet-archive"), "Internet Archive")
        self.assertEqual(fmt.provider_name("tosec", {"tosec": "TOSEC"}), "TOSEC")

    def test_queue_key_matches_the_write_queue(self) -> None:
        disk = fmt.new_queue_item("A", Platform.AMIGA, disk_id=3, image_id=31)
        other_dump = fmt.new_queue_item("A", Platform.AMIGA, disk_id=3)
        local = LocalFile(path="/x/y.zip", member="a.adf")
        file_item = fmt.new_queue_item("a.adf", None, local=local)
        self.assertEqual(fmt.queue_key(disk), fmt.queue_key(other_dump))
        self.assertEqual(fmt.queue_key(file_item), ("file", "/x/y.zip", "a.adf"))

    def test_catalogue_stats_line(self) -> None:
        text = fmt.catalogue_stats_text({"disks": 12345, "series": 180}, "2026-09-01")
        self.assertEqual(text, "12,345 disks in 180 series. Built 1 Sep 2026.")


class HelpContentTests(unittest.TestCase):
    def test_topics_are_complete_and_plain(self) -> None:
        slugs = [topic.slug for topic in HELP_TOPICS]
        self.assertEqual(len(slugs), len(set(slugs)))
        for wanted in (
            "searching",
            "writing",
            "library",
            "downloads",
            "formats",
            "troubleshooting",
        ):
            self.assertIn(wanted, slugs)
        for topic in HELP_TOPICS:
            for section in topic.sections:
                for text in (section.heading, *section.paragraphs, *section.steps):
                    self.assertTrue(text.isascii(), text)

    def test_troubleshooting_covers_the_usual_problems(self) -> None:
        topic = next(topic for topic in HELP_TOPICS if topic.slug == "troubleshooting")
        text = " ".join(" ".join(section.paragraphs) for section in topic.sections).lower()
        for words in ("write-protect", "no index", "after the twist"):
            self.assertIn(words, text)

    def test_example_searches_and_credits_are_data(self) -> None:
        self.assertTrue(EXAMPLE_SEARCHES)
        names = {name for name, _url, _what in DATA_CREDITS}
        self.assertTrue({"TOSEC", "Atari Legend", "Internet Archive"} <= names)


if __name__ == "__main__":
    unittest.main()


class DriveChoiceTests(unittest.TestCase):
    def test_every_drive_the_writer_accepts_can_be_chosen(self) -> None:
        from piratefinder.greaseweazle.client import DRIVES

        self.assertEqual([code for code, _name, _about in fmt.DRIVES], list(DRIVES))
