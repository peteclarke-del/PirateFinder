"""TOSEC name parsing and text normalisation."""

from __future__ import annotations

import unittest

from piratefinder.catalogue.naming import (
    display_title,
    image_rank_key,
    normalise,
    parse_tosec_name,
    sort_key,
    split_combined,
    tidy_label,
)


class ParseTosecNameTest(unittest.TestCase):
    def test_menu_disk_with_version_flags_and_extension(self) -> None:
        name = parse_tosec_name("Automation Menu Disk 000 v2.0 (19xx)(Automation)[m LGD][a].st")
        self.assertEqual(name.title, "Automation Menu Disk 000")
        self.assertEqual(name.version, "v2.0")
        self.assertEqual(name.date, "19xx")
        self.assertEqual(name.publisher, "Automation")
        self.assertEqual(name.flags, ("m LGD", "a"))
        self.assertEqual(name.modified_by, "LGD")
        self.assertEqual(name.alternate, 1)
        self.assertEqual(name.extension, "st")
        self.assertTrue(name.modified)
        self.assertFalse(name.bad)

    def test_media_fields_give_the_part(self) -> None:
        name = parse_tosec_name("Automation Menu Disk 069 (1989)(Automation)(Disk 1 of 5)(Part A)")
        self.assertEqual(name.extra, ("Disk 1 of 5", "Part A"))
        self.assertEqual(name.part, "A")
        self.assertEqual(name.disk_of, (1, 5))
        both = "Automation Menu Disk 500 (19xx)(Automation)(Disk 13 of 15)(Part L Disk B)"
        self.assertEqual(parse_tosec_name(both).part, "LB")
        titled = (
            "Automation Menu Disk 500 (19xx)(Automation)(Disk 07 of 15)(Part F - Shadow Player)"
        )
        self.assertEqual(parse_tosec_name(titled).part, "F")
        self.assertEqual(
            parse_tosec_name("D-Bug Menu Disk 100 (19xx)(D-Bug)(Disk 2 of 5)").part, "2"
        )

    def test_crack_trainer_and_dump_flags(self) -> None:
        name = parse_tosec_name("Xenon (1988)(Melbourne House)[cr][t +2 Avengers]")
        self.assertEqual(name.cracker, "")
        self.assertTrue(name.cracked)
        self.assertEqual(name.trainer, "t +2 Avengers")
        self.assertEqual(name.trainer_count, "+2")
        self.assertEqual(name.trainer_group, "Avengers")
        bad = parse_tosec_name("Compact #031 (1991)(Skid Row)[b errdms]")
        self.assertTrue(bad.bad)
        verified = parse_tosec_name("Game (1990)(Pub)[!][a2]")
        self.assertTrue(verified.verified)
        self.assertEqual(verified.alternate, 2)

    def test_translation_is_not_a_trainer_and_monochrome_is_not_modified(self) -> None:
        name = parse_tosec_name("Game (1990)(Pub)[tr de][monochrome]")
        self.assertEqual(name.trainer, "")
        self.assertFalse(name.modified)

    def test_demo_field_and_unknown_publisher(self) -> None:
        name = parse_tosec_name("Backlash (demo-playable) (1987)(-)")
        self.assertEqual(name.title, "Backlash")
        self.assertEqual(name.publisher, "")
        self.assertEqual(name.extra, ("demo-playable",))

    def test_combined_entry(self) -> None:
        text = (
            "Speedball 2 - Brutal Deluxe (1990)(Image Works)[cr Replicants - ST Amigos][t] & "
            "Exterminator (1990)(Audiogenic)[cr Masters - Replicants - ST Amigos][t]"
        )
        self.assertEqual(len(split_combined(text)), 2)
        name = parse_tosec_name(text)
        self.assertEqual(name.title, "Speedball 2 - Brutal Deluxe & Exterminator")
        self.assertEqual(name.cracker, "Replicants - ST Amigos - Masters")
        self.assertEqual(name.publisher, "Image Works - Audiogenic")

    def test_whole_entry_flag_after_a_dash(self) -> None:
        name = parse_tosec_name("Arkanoid II (1988)(Imagine) & Wizball (1987)(Ocean)-[a]")
        self.assertEqual(name.alternate, 1)
        self.assertEqual(name.title, "Arkanoid II & Wizball")

    def test_plain_file_names_still_parse(self) -> None:
        name = parse_tosec_name("Automation 250.msa")
        self.assertEqual((name.title, name.extension, name.date), ("Automation 250", "msa", ""))
        self.assertEqual(
            parse_tosec_name("Great Sample Collection Vol.1").title, "Great Sample Collection Vol.1"
        )


class TextTest(unittest.TestCase):
    def test_normalise(self) -> None:
        self.assertEqual(normalise("Nigel Mansell's Grand Prix"), "nigel mansells grand prix")
        self.assertEqual(normalise("\u00c9lan & Co.  D-Bug"), "elan and co d bug")
        self.assertEqual(normalise("Automation #250"), "automation 250")

    def test_sort_key_orders_numbers_naturally(self) -> None:
        labels = ["Automation 10", "Automation 9", "Automation 100"]
        self.assertEqual(
            sorted(labels, key=sort_key), ["Automation 9", "Automation 10", "Automation 100"]
        )

    def test_display_title_moves_the_article(self) -> None:
        self.assertEqual(display_title("Chaos Engine, The"), "The Chaos Engine")
        self.assertEqual(display_title("Chaos Engine, The - Demo"), "The Chaos Engine - Demo")
        self.assertEqual(display_title("Chaos Engine, The CD32"), "The Chaos Engine CD32")
        self.assertEqual(display_title("Walk, A Story"), "Walk, A Story")

    def test_tidy_label(self) -> None:
        self.assertEqual(
            tidy_label("Chaos Engine, The (1993)(Renegade)(Disk 1 of 2)[cr Cynix][a]"),
            "The Chaos Engine (Disk 1 of 2) [cr Cynix]",
        )


class RankTest(unittest.TestCase):
    def test_verified_then_clean_then_alternates_then_changed_then_bad(self) -> None:
        flags = ["[b]", "[t]", "[a2]", "", "[a]", "[!]", "[m LGD][a]"]
        ordered = sorted(flags, key=image_rank_key)
        self.assertEqual(ordered, ["[!]", "", "[a]", "[a2]", "[t]", "[m LGD][a]", "[b]"])


if __name__ == "__main__":
    unittest.main()
