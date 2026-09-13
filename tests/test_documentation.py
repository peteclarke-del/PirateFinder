"""The published documentation follows the house style and its links resolve.

Documents are written in plain ASCII punctuation. A dash or an arrow pasted in
from elsewhere is easy to miss in review and then appears in every rendered
copy, so the characters are checked here rather than by eye.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

FORBIDDEN = {
    "\N{EM DASH}": "em dash",
    "\N{EN DASH}": "en dash",
    "\N{HORIZONTAL ELLIPSIS}": "ellipsis",
    "\N{LEFT SINGLE QUOTATION MARK}": "curly quote",
    "\N{RIGHT SINGLE QUOTATION MARK}": "curly quote",
    "\N{LEFT DOUBLE QUOTATION MARK}": "curly quote",
    "\N{RIGHT DOUBLE QUOTATION MARK}": "curly quote",
}
# Arrows, Supplemental Arrows-A and -B, Dingbat arrows and Miscellaneous
# Symbols and Arrows.
ARROWS = re.compile(r"[\u2190-\u21ff\u27f0-\u27ff\u2900-\u297f\u2794-\u27bf\u2b00-\u2bff]")
# Stock phrases that say little; each is matched at the start of a word.
PHRASES = (
    "seamless",
    "leverage",
    "robust",
    "delve",
    "comprehensive",
    "streamline",
    "effortless",
    "empower",
    "powerful",
    "intuitive",
    "elevate",
    "unlock",
    "harness",
    "crucial",
    "vital",
    "simply",
    "just works",
    "whether you are",
    "key features",
)
USER_GUIDE = DOCS / "USER_GUIDE.md"
HELP_PICTURES = ROOT / "src" / "piratefinder" / "data" / "help"
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def published_documents() -> list[Path]:
    """Markdown the project publishes, plus text shown by the desktop."""
    files = sorted(ROOT.glob("*.md")) + sorted(DOCS.glob("*.md"))
    files += sorted((ROOT / ".github").rglob("*.md"))
    files += sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"))
    files += sorted((ROOT / "data").glob("*.desktop"))
    files += sorted((ROOT / "data").glob("*.metainfo.xml"))
    files += [ROOT / "NOTICE"]
    # The in-app User Guide is published text too.
    help_text = ROOT / "src" / "piratefinder" / "ui" / "help_content.py"
    if help_text.is_file():
        files.append(help_text)
    help_dir = ROOT / "src" / "piratefinder" / "data" / "help"
    if help_dir.is_dir():
        files += sorted(path for path in help_dir.rglob("*") if path.is_file())
    return [path for path in files if path.suffix not in (".png", ".jpg", ".svg")]


def local_links(path: Path) -> list[str]:
    targets = []
    for raw in LINK.findall(path.read_text(encoding="utf-8")):
        target = unquote(raw.strip("<>").split("#", 1)[0])
        if not target or "://" in target or target.startswith(("mailto:", "/")):
            continue
        targets.append(target)
    return targets


class PunctuationTests(unittest.TestCase):
    def test_no_dashes_ellipses_or_arrows(self) -> None:
        offenders = []
        for path in published_documents():
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                found = [name for char, name in FORBIDDEN.items() if char in line]
                if ARROWS.search(line):
                    found.append("arrow")
                if found:
                    location = f"{path.relative_to(ROOT)}:{number}"
                    offenders.append(f"{location}: {', '.join(found)}")
        self.assertEqual([], offenders)

    def test_no_stock_filler_words(self) -> None:
        offenders = []
        for path in published_documents():
            text = path.read_text(encoding="utf-8").lower()
            offenders += [
                f"{path.relative_to(ROOT)}: {word}"
                for word in PHRASES
                if re.search(rf"\b{word}", text)
            ]
        self.assertEqual([], offenders)


class LinkTests(unittest.TestCase):
    def test_local_links_resolve(self) -> None:
        missing = [
            f"{path.relative_to(ROOT)} -> {target}"
            for path in published_documents()
            if path.suffix == ".md"
            for target in local_links(path)
            if not (path.parent / target).exists()
        ]
        self.assertEqual([], missing)

    def test_every_document_is_linked_from_the_readme_or_a_docs_index(self) -> None:
        linked = {(ROOT / target).resolve() for target in local_links(ROOT / "README.md")}
        for index_name in ("README.md", "index.md"):
            index = DOCS / index_name
            if index.is_file():
                linked |= {(DOCS / target).resolve() for target in local_links(index)}
        unlinked = [
            str(path.relative_to(ROOT))
            for path in sorted(DOCS.glob("*.md"))
            if path.resolve() not in linked and path.name not in ("README.md", "index.md")
        ]
        self.assertEqual([], unlinked)

    def test_the_readme_links_the_project_documents(self) -> None:
        targets = set(local_links(ROOT / "README.md"))
        for required in (
            "docs/README.md",
            "docs/USER_GUIDE.md",
            "docs/TROUBLESHOOTING.md",
            "docs/PRIVACY.md",
            "docs/INSTALLATION.md",
            "docs/DATA_SOURCES.md",
            "docs/FORMAT_SUPPORT.md",
            "docs/CURRENT_STATUS.md",
            "docs/RELEASING.md",
            "ROADMAP.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "THIRD_PARTY_NOTICES.md",
        ):
            self.assertIn(required, targets)


class UserGuideTests(unittest.TestCase):
    """docs/USER_GUIDE.md is the in-app guide, with the same pictures."""

    def test_the_markdown_guide_is_up_to_date(self) -> None:
        from piratefinder.ui.help_content import guide_markdown

        self.assertEqual(
            USER_GUIDE.read_text(encoding="utf-8"),
            guide_markdown(),
            "run: PYTHONPATH=src python3 -m piratefinder.ui.help_content",
        )

    def test_every_picture_the_guide_shows_exists(self) -> None:
        from piratefinder.ui.help_content import help_images

        names = help_images()
        self.assertTrue(names)
        for name in names:
            self.assertTrue((HELP_PICTURES / name).is_file(), f"data/help/{name}")
            self.assertTrue((DOCS / "images" / name).is_file(), f"docs/images/{name}")
        shipped = {path.name for path in HELP_PICTURES.glob("*.png")}
        self.assertEqual(sorted(shipped - set(names)), [], "pictures the guide does not show")

    def test_the_search_examples_name_the_discs_the_guide_says(self) -> None:
        """The disc references in the guide use aliases from data/series/series.toml."""
        import tomllib

        from piratefinder.catalogue.naming import normalise
        from piratefinder.catalogue.search import parse_query

        with (ROOT / "data" / "series" / "series.toml").open("rb") as handle:
            declared = tomllib.load(handle)["series"]
        aliases: dict[str, tuple[str, ...]] = {}
        for series in declared:
            for name in (series["name"], *series.get("aliases", ())):
                key = normalise(name)
                aliases[key] = (*aliases.get(key, ()), series["id"])
        for text, series_id, number, part, version in (
            ("a250", "automation", 250, "", ""),
            ("auto250", "automation", 250, "", ""),
            ("automation #250", "automation", 250, "", ""),
            ("pp 51", "pompey-pirates", 51, "", ""),
            ("pompey pirates menu disk 51", "pompey-pirates", 51, "", ""),
            ("mb 60", "medway-boys", 60, "", ""),
            ("sr 128", "skid-row-compact", 128, "", ""),
            ("sgau 100", "supergau", 100, "", ""),
            ("d-bug 100b", "d-bug", 100, "B", ""),
            ("dbug 100 part b", "d-bug", 100, "B", ""),
            ("automation 100 v2", "automation", 100, "", "v2"),
        ):
            with self.subTest(text=text):
                parsed = parse_query(text, aliases)
                self.assertIn(series_id, parsed.series_ids)
                self.assertEqual(
                    (parsed.number, parsed.part, parsed.version), (number, part, version)
                )
        guide = USER_GUIDE.read_text(encoding="utf-8")
        for text in ("`a250`", "`pp 51`", "`sr 128`", "`d-bug 100b`", "`automation 100 v2`"):
            self.assertIn(text, guide)

    def test_guide_text_is_plain(self) -> None:
        """Outside `code`, no character that Markdown or Pango would take for markup."""
        from piratefinder.ui.help_content import HELP_TOPICS

        texts = []
        for topic in HELP_TOPICS:
            texts += [topic.title, topic.summary, topic.screenshot_alt]
            for section in topic.sections:
                texts += [section.heading, *section.paragraphs, *section.steps]
                texts += [*section.bullets, *section.columns, section.screenshot_alt]
                texts += [text for pair in section.terms for text in pair]
        offenders = []
        for text in texts:
            self.assertEqual(text.count("`") % 2, 0, text)
            prose = "".join(text.split("`")[0::2])
            if re.search(r"[<>*|!]", prose) or not text.isascii():
                offenders.append(text)
        self.assertEqual([], offenders)


class RepositoryFilesTests(unittest.TestCase):
    def test_governance_and_licence_files_exist(self) -> None:
        for relative in (
            "LICENSE",
            "NOTICE",
            "README.md",
            "ROADMAP.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "GOVERNANCE.md",
            "SECURITY.md",
            "SUPPORT.md",
            "THIRD_PARTY_NOTICES.md",
            ".github/CODEOWNERS",
            ".github/dependabot.yml",
            ".github/pull_request_template.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_the_licence_is_the_gpl_version_3(self) -> None:
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", licence)
        self.assertIn("Version 3, 29 June 2007", licence)
        self.assertIn("END OF TERMS AND CONDITIONS", licence)

    def test_the_licensing_boundary_is_stated(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for text in (readme, notices):
            self.assertIn("GPL-3.0-or-later", text)
            self.assertIn("CC BY-NC-SA 4.0", text)
        self.assertIn("Unlicense", notices)
        self.assertIn("Do not open a public issue", (ROOT / "SECURITY.md").read_text("utf-8"))

    def test_bundled_python_package_versions_match_the_pins(self) -> None:
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        pins = (ROOT / "packaging" / "runtime-requirements.txt").read_text(encoding="utf-8")
        for line in pins.splitlines():
            if "==" in line:
                name, version = line.strip().split("==", 1)
                self.assertIn(f"| {name} | {version} |", notices)
        greaseweazle = (ROOT / "packaging" / "greaseweazle-version.txt").read_text("utf-8")
        self.assertIn(f"| Greaseweazle host tools | {greaseweazle.strip()} |", notices)


if __name__ == "__main__":
    unittest.main()
