"""Text the interface shows, tested without a display."""

from __future__ import annotations

import unittest
from dataclasses import replace

from piratefinder.library.library import clean_names, local_name
from piratefinder.models import (
    Content,
    ContentKind,
    Disk,
    DiskKind,
    LocalFile,
    MediaItem,
    Platform,
    Query,
    ResultMode,
    ResultPage,
    ScanSummary,
    SessionSummary,
    TriviaItem,
    VirusReport,
    VirusStatus,
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


class FindTextTests(unittest.TestCase):
    def test_words_that_start_with_a_query_word_are_bold(self) -> None:
        words = fmt.query_words("rick D&")
        self.assertEqual(words, ["rick", "d"])
        self.assertEqual(
            fmt.highlight_markup("Rick Dangerous & Brick", words),
            "<b>Rick</b> <b>D</b>angerous &amp; Brick",
        )
        self.assertEqual(fmt.highlight_markup("<Menu>", []), "&lt;Menu&gt;")

    def test_pager_range(self) -> None:
        disk = Disk(1, "Automation 250", Platform.ATARI_ST, DiskKind.MENU)
        from piratefinder.models import Availability, ResultRow

        rows = tuple(ResultRow(disk, Availability.LOCAL, title=f"T{i}") for i in range(100))
        page = ResultPage(Query(page=1, page_size=100), rows, 1234)
        self.assertEqual(fmt.range_text(page), "101 to 200 of 1,234 titles")
        self.assertEqual(page.pages, 13)
        few = ResultPage(Query(mode=ResultMode.DISCS), rows[:3], 3)
        self.assertEqual(fmt.range_text(few), "3 discs")
        self.assertEqual(fmt.range_text(ResultPage(Query(), (), 0)), "No titles")

    def test_selection_counts(self) -> None:
        self.assertEqual(fmt.selection_text(3, 3, 1), "3 selected")
        self.assertEqual(fmt.selection_text(3, 2, 2), "3 titles selected on 2 discs across 2 pages")

    def test_release_dates(self) -> None:
        self.assertEqual(fmt.release_text(1989, 6, 17), "17 June 1989")
        self.assertEqual(fmt.release_text(1989, 6), "June 1989")
        self.assertEqual(fmt.release_text(1989), "1989")
        self.assertEqual(fmt.release_text(None), "Unknown")
        # The day comes from the disc, never from its date text.
        disk = Disk(1, "PP 1", Platform.ATARI_ST, DiskKind.MENU, date="1989", year=1989, month=6)
        self.assertEqual(fmt.disk_release(replace(disk, day=17)), "17 June 1989")
        self.assertEqual(fmt.disk_release(replace(disk, date="1989-06-17")), "June 1989")
        self.assertEqual(
            (fmt.disk_year(disk), fmt.disk_year(replace(disk, year=None))), ("1989", "")
        )
        self.assertEqual(fmt.disk_release(replace(disk, year=None, month=None)), "Unknown")

    def test_credits_name_their_source_and_licence(self) -> None:
        summary = TriviaItem(
            "summary", "Text", "wikipedia", licence="CC BY-SA 4.0", title="Rick Dangerous"
        )
        self.assertEqual(
            fmt.trivia_credit(summary, {"wikipedia": "Wikipedia"}),
            "From Wikipedia, Rick Dangerous, CC BY-SA 4.0",
        )
        fact = TriviaItem("fact", "Text", "atari-legend")
        self.assertEqual(fmt.trivia_credit(fact), "Source: Atari Legend")
        item = MediaItem("menu", "u", "atari-legend", credit="Photo by someone")
        self.assertEqual(fmt.media_credit(item), "Photo by someone, Atari Legend")
        named = MediaItem("menu", "u", "atari-legend", credit="Screenshot: Atari Legend, CC BY")
        self.assertEqual(fmt.media_credit(named), "Screenshot: Atari Legend, CC BY")
        self.assertEqual(
            fmt.media_caption(item, 0, 3, "Automation 250"), "Menu screen of Automation 250, 1 of 3"
        )

    def test_virus_text_says_what_can_be_done(self) -> None:
        boot = VirusReport(VirusStatus.VIRUS, "SCA", "boot", True, source="built-in")
        self.assertEqual(fmt.virus_heading(boot), "Virus Found: SCA")
        self.assertIn("standard boot block", fmt.virus_body(boot))
        self.assertEqual(fmt.virus_source(boot), "Identified by PirateFinder's built-in signatures")
        # The detector's own explanation is shown as it is.
        explained = VirusReport(VirusStatus.VIRUS, "SCA", "boot", True, "It spreads.", "built-in")
        self.assertEqual(fmt.virus_body(explained), "It spreads.")
        flagged = VirusReport(VirusStatus.FLAGGED, "Saddam", "file", False, source="TOSEC")
        self.assertEqual(fmt.virus_heading(flagged), "Dump Flagged with Saddam")
        body = fmt.virus_body(flagged)
        self.assertTrue(body.startswith("TOSEC lists this dump as carrying Saddam, a file virus."))
        self.assertIn("cannot remove it", body)
        self.assertEqual(fmt.virus_source(flagged), "Listed by TOSEC")

    def test_cleaning_explains_what_happens_to_the_file(self) -> None:
        plain = LocalFile(path="/lib/Automation 250.st", format="st")
        self.assertIn(
            "kept beside it as Automation 250.st.bak", fmt.clean_explanation(plain, "SCA")
        )
        member = LocalFile(path="/lib/amiga.zip", member="DISK2.ADF", format="adf")
        text = fmt.clean_explanation(member, "Byte Bandit")
        self.assertIn("inside amiga.zip, which is not changed", text)
        packed = LocalFile(path="/lib/game.dms", format="dms")
        self.assertIn("game (cleaned).adf", fmt.clean_explanation(packed, "SCA"))
        # "gz" names no platform (a gzipped ADF is "adz"), so the disc's platform decides.
        gzipped = LocalFile(path="/lib/game.gz", format="gz")
        text = fmt.clean_explanation(gzipped, "Ghost", Platform.ATARI_ST)
        self.assertIn("game (cleaned).st", text)

    def test_cleaning_names_the_file_the_library_writes(self) -> None:
        # The library drops the gzip suffix and names the copy for the image inside.
        gzipped = LocalFile(path="/lib/game.st.gz", format="gz")
        text = fmt.clean_explanation(gzipped, "Ghost", Platform.ATARI_ST)
        self.assertIn("beside game.st.gz as game (cleaned).st.", text)
        names = clean_names(gzipped, "gz", Platform.ATARI_ST)
        self.assertEqual((names.cleaned, names.backup), ("game (cleaned).st", ""))
        plain = clean_names(LocalFile(path="/lib/Game.ST"), "st", None)
        self.assertEqual((plain.cleaned, plain.backup), ("Game.ST", "Game.ST.bak"))

    def test_a_member_is_named_by_its_own_file_name(self) -> None:
        nested = LocalFile(path="/lib/set.7z", member="inner.zip::menus\\DISK2.ADF", format="adf")
        self.assertTrue(
            fmt.clean_explanation(nested, "SCA").startswith("DISK2.ADF is inside set.7z")
        )
        self.assertEqual(local_name(nested), "DISK2.ADF")
        self.assertEqual(local_name(replace(nested, display_name="Disk 2")), "Disk 2")
        self.assertEqual(local_name(LocalFile(path="/lib/x.st")), "x.st")


class DurationTextTests(unittest.TestCase):
    def test_an_estimate_is_rounded_to_what_a_person_plans_by(self) -> None:
        self.assertEqual(fmt.duration_text(20), "under a minute")
        self.assertEqual(fmt.duration_text(60), "about 1 minute")
        self.assertEqual(fmt.duration_text(89 * 60), "about 89 minutes")
        self.assertEqual(fmt.duration_text(90 * 60 - 1), "about 2 hours")
        self.assertEqual(fmt.duration_text(90 * 60), "about 2 hours")
        self.assertEqual(fmt.duration_text(41_447), "about 12 hours")


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

    def test_a_summary_row_lists_the_result_details_and_notes(self) -> None:
        outcome = WriteOutcome(
            WriteStatus.VERIFIED,
            "Written and verified.",
            retries=2,
            notes=("Decoded the DMS archive to an ADF image.", "The SCA virus was removed."),
        )
        self.assertEqual(
            fmt.outcome_subtitle(outcome, "Local file /nas/a.dms"),
            "Written and verified\n2 retries • Local file /nas/a.dms\n"
            "Decoded the DMS archive to an ADF image.\nThe SCA virus was removed.",
        )
        self.assertEqual(fmt.outcome_subtitle(WriteOutcome(WriteStatus.SKIPPED, ""), ""), "Skipped")


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


class DetailTextTests(unittest.TestCase):
    def test_the_disc_subtitle_names_the_date_in_words(self) -> None:
        disk = Disk(
            1,
            "Automation 400",
            Platform.ATARI_ST,
            DiskKind.MENU,
            series_name="Automation",
            date="1991-06",
            year=1991,
            month=6,
        )
        self.assertEqual(fmt.disk_subtitle(disk), "Automation • June 1991 • Atari ST • Menu disk")
        undated = replace(disk, date="", year=None, month=None)
        self.assertEqual(fmt.disk_subtitle(undated), "Automation • Atari ST • Menu disk")

    def test_a_boot_block_that_is_not_a_virus(self) -> None:
        loader = VirusReport(VirusStatus.KNOWN_BOOT, name="TOS boot loader")
        self.assertEqual(fmt.boot_block_text(loader), "TOS boot loader. It is not a virus.")
        explained = VirusReport(VirusStatus.UNKNOWN_BOOT, explanation="Code nobody knows. ")
        self.assertEqual(fmt.boot_block_text(explained), "Code nobody knows.")
        guard = VirusReport(VirusStatus.ANTIVIRUS, name="SCA Protector")
        self.assertIn("anti-virus boot block", fmt.boot_block_text(guard))
        for status in (VirusStatus.CLEAN, VirusStatus.VIRUS, VirusStatus.FLAGGED):
            self.assertEqual(fmt.boot_block_text(VirusReport(status, name="X")), "")
        self.assertEqual(fmt.boot_block_text(None), "")

    def test_text_laid_out_for_a_fixed_width_font(self) -> None:
        menu = "   FUZION PRESENTS\n   ---------------\n F1 - HUDSON HAWK\n F2 - SPEEDBALL"
        self.assertTrue(fmt.laid_out(menu))
        art = "Original bbs file listing;\n      ________    /\n .--_/ __/__ ___/__  /"
        self.assertTrue(fmt.laid_out(art))
        self.assertFalse(fmt.laid_out("Release date is an estimate."))
        self.assertFalse(fmt.laid_out("Level codes:\nLEVEL 10 = GOLD\nLEVEL 20 = FISH"))
        self.assertFalse(fmt.laid_out("One line ------ with a rule"))


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

    def test_catalogue_stats_line(self) -> None:
        text = fmt.catalogue_stats_text({"disks": 12345, "series": 180}, "2026-09-01")
        self.assertEqual(text, "12,345 disks in 180 series. Built 1 Sep 2026.")


class HelpContentTests(unittest.TestCase):
    def test_topics_are_complete_and_plain(self) -> None:
        slugs = [topic.slug for topic in HELP_TOPICS]
        self.assertEqual(len(slugs), len(set(slugs)))
        for wanted in (
            "overview",
            "searching",
            "details",
            "writing",
            "viruses",
            "library",
            "downloads",
            "preferences",
            "catalogue",
            "formats",
            "troubleshooting",
            "shortcuts",
            "credits",
            "privacy",
        ):
            self.assertIn(wanted, slugs)
        for topic in HELP_TOPICS:
            self.assertTrue(topic.sections, topic.slug)
            for section in topic.sections:
                terms = [text for pair in section.terms for text in pair]
                for text in (
                    section.heading,
                    *section.paragraphs,
                    *section.steps,
                    *section.bullets,
                    *terms,
                ):
                    self.assertTrue(text.isascii(), text)
                if section.terms:
                    self.assertTrue(all(section.columns), section.heading)

    def test_shortcut_labels_are_readable(self) -> None:
        from piratefinder.ui.help_content import SHORTCUTS, accelerator_label

        self.assertEqual(accelerator_label("<Control>Page_Down"), "Ctrl+Page Down")
        self.assertEqual(accelerator_label("<Control>f"), "Ctrl+F")
        self.assertEqual(accelerator_label("<Alt>1"), "Alt+1")
        self.assertEqual(accelerator_label("F1"), "F1")
        topic = next(topic for topic in HELP_TOPICS if topic.slug == "shortcuts")
        shown = {text for section in topic.sections for _keys, text in section.terms}
        for _group, entries in SHORTCUTS:
            for _keys, text in entries:
                self.assertIn(text, shown)

    def test_troubleshooting_covers_the_usual_problems(self) -> None:
        topic = next(topic for topic in HELP_TOPICS if topic.slug == "troubleshooting")
        text = " ".join(" ".join(section.paragraphs) for section in topic.sections).lower()
        for words in ("write-protect", "no index", "after the twist"):
            self.assertIn(words, text)

    def test_example_searches_and_credits_are_data(self) -> None:
        self.assertTrue(EXAMPLE_SEARCHES)
        names = {name for name, _url, _what in DATA_CREDITS}
        self.assertTrue({"TOSEC", "Atari Legend", "Internet Archive"} <= names)


class DriveChoiceTests(unittest.TestCase):
    def test_every_drive_the_writer_accepts_can_be_chosen(self) -> None:
        from piratefinder.greaseweazle.client import DRIVES

        self.assertEqual([code for code, _name, _about in fmt.DRIVES], list(DRIVES))


class CleanedTextTests(unittest.TestCase):
    def test_the_message_says_what_happened_to_the_original(self) -> None:
        original = LocalFile(path="/nas/Game.adf")
        self.assertIn(".bak", fmt.cleaned_text(original, original))
        copy = LocalFile(path="/nas/Game (cleaned).adf")
        text = fmt.cleaned_text(LocalFile(path="/nas/Game.dms"), copy)
        self.assertIn("Game (cleaned).adf", text)
        self.assertIn("unchanged", text)
        self.assertNotIn(".bak", text)


if __name__ == "__main__":
    unittest.main()
