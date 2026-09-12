"""The desktop metadata, launcher and package builder agree with each other.

These files are read by different programs (the desktop shell, AppStream,
dpkg, udev, the shell running the launcher) and nothing at run time checks
that they describe the same application. A renamed icon or a launcher that
loses its PATH line produces a package that installs cleanly and then fails.
"""

from __future__ import annotations

import configparser
import re
import runpy
import tomllib
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from piratefinder import __version__, branding

ROOT = Path(__file__).resolve().parents[1]
APP_ID = branding.APPLICATION_ID
DESKTOP_FILE = ROOT / "data" / f"{APP_ID}.desktop"
METAINFO_FILE = ROOT / "data" / f"{APP_ID}.metainfo.xml"
ICONS = ROOT / "src" / "piratefinder" / "data" / "icons" / "hicolor"
COLOUR_ICON = ICONS / "scalable" / "apps" / f"{APP_ID}.svg"
SYMBOLIC_ICON = ICONS / "symbolic" / "apps" / f"{APP_ID}-symbolic.svg"
PACKAGING = ROOT / "packaging"
BUILD_DEB = PACKAGING / "build-deb.sh"
SVG = "{http://www.w3.org/2000/svg}"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class DesktopEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # keys are case sensitive
        parser.read(DESKTOP_FILE, encoding="utf-8")
        self.entry = parser["Desktop Entry"]

    def test_the_entry_launches_the_packaged_command(self) -> None:
        self.assertEqual(self.entry["Type"], "Application")
        self.assertEqual(self.entry["Exec"], "piratefinder")
        self.assertEqual(self.entry["Terminal"], "false")
        self.assertEqual(self.entry["StartupNotify"], "true")
        self.assertEqual(self.entry["Name"], branding.APPLICATION_NAME)

    def test_the_icon_is_named_after_the_application_id(self) -> None:
        self.assertEqual(self.entry["Icon"], APP_ID)
        self.assertTrue(COLOUR_ICON.is_file())
        self.assertTrue(SYMBOLIC_ICON.is_file())

    def test_categories_and_keywords(self) -> None:
        self.assertEqual(self.entry["Categories"], "Utility;")
        keywords = self.entry["Keywords"].rstrip(";").split(";")
        for keyword in ("floppy", "disk", "Amiga", "Atari", "Greaseweazle", "menu", "compact"):
            self.assertIn(keyword, keywords)

    def test_the_entry_claims_no_file_types(self) -> None:
        self.assertNotIn("MimeType", self.entry)


class MetainfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ElementTree.parse(METAINFO_FILE).getroot()

    def test_the_component_id_matches_the_application_id(self) -> None:
        self.assertEqual(self.root.get("type"), "desktop-application")
        self.assertEqual(self.root.findtext("id"), APP_ID)
        self.assertEqual(self.root.findtext("name"), branding.APPLICATION_NAME)
        launchable = self.root.find("launchable")
        self.assertIsNotNone(launchable)
        self.assertEqual(launchable.text, f"{APP_ID}.desktop")

    def test_licences_and_links(self) -> None:
        self.assertEqual(self.root.findtext("project_license"), "GPL-3.0-or-later")
        self.assertEqual(self.root.findtext("metadata_license"), "CC0-1.0")
        urls = {url.get("type"): url.text for url in self.root.findall("url")}
        self.assertEqual(urls["homepage"], branding.HOMEPAGE)
        self.assertEqual(urls["bugtracker"], f"{branding.HOMEPAGE}/issues")

    def test_the_newest_release_is_the_package_version(self) -> None:
        releases = self.root.findall("releases/release")
        self.assertTrue(releases)
        self.assertEqual(releases[0].get("version"), __version__)
        self.assertRegex(releases[0].get("date", ""), r"^\d{4}-\d{2}-\d{2}$")

    def test_the_pyproject_licence_matches(self) -> None:
        project = tomllib.loads(read(ROOT / "pyproject.toml"))["project"]
        self.assertEqual(project["license"], self.root.findtext("project_license"))


class IconTests(unittest.TestCase):
    def test_the_icons_are_well_formed_svg(self) -> None:
        for path in (COLOUR_ICON, SYMBOLIC_ICON):
            with self.subTest(icon=path.name):
                root = ElementTree.parse(path).getroot()
                self.assertEqual(root.tag, f"{SVG}svg")
                self.assertIn(root.get("viewBox"), ("0 0 128 128", "0 0 16 16"))

    def test_the_colour_icon_uses_the_128_pixel_canvas(self) -> None:
        root = ElementTree.parse(COLOUR_ICON).getroot()
        self.assertEqual(root.get("viewBox"), "0 0 128 128")

    def test_the_symbolic_icon_is_one_colour(self) -> None:
        root = ElementTree.parse(SYMBOLIC_ICON).getroot()
        self.assertEqual(root.get("viewBox"), "0 0 16 16")
        colours = set(re.findall(r"#[0-9a-fA-F]{3,6}\b", read(SYMBOLIC_ICON)))
        self.assertEqual(colours, {"#2e3436"})

    def test_the_icons_are_package_data(self) -> None:
        tool = tomllib.loads(read(ROOT / "pyproject.toml"))["tool"]
        patterns = tool["setuptools"]["package-data"]["piratefinder"]
        self.assertIn("data/icons/hicolor/*/apps/*", patterns)


class LauncherTests(unittest.TestCase):
    def test_the_installed_launcher_guards_against_snap_toolkit_paths(self) -> None:
        for path in (PACKAGING / "piratefinder", ROOT / "piratefinder"):
            with self.subTest(launcher=str(path.relative_to(ROOT))):
                text = read(path)
                self.assertIn("/snap/", text)
                for variable in (
                    "GTK_PATH",
                    "GTK_EXE_PREFIX",
                    "GDK_PIXBUF_MODULE_FILE",
                    "GIO_MODULE_DIR",
                    "GSETTINGS_SCHEMA_DIR",
                    "XDG_DATA_DIRS",
                ):
                    self.assertIn(variable, text)
                self.assertIn("-m piratefinder", text)

    def test_the_private_gw_comes_first_on_path(self) -> None:
        text = read(PACKAGING / "piratefinder")
        self.assertIn("application_lib=/usr/lib/piratefinder", text)
        self.assertIn('export PATH="${application_lib}/bin:${PATH}"', text)
        self.assertIn("exec /usr/bin/python3 -m piratefinder", text)
        self.assertLess(text.index("export PATH="), text.index("exec "))

    def test_the_gw_wrapper_runs_the_bundled_host_tools(self) -> None:
        wrapper = read(PACKAGING / "gw")
        self.assertIn("application_lib=/usr/lib/piratefinder", wrapper)
        self.assertIn('"${application_lib}/gw_entry.py"', wrapper)
        self.assertIn("from greaseweazle.cli import main", read(PACKAGING / "gw_entry.py"))

    def test_maintainer_scripts_refresh_udev_desktop_and_icon_caches(self) -> None:
        for name in ("postinst", "postrm"):
            with self.subTest(script=name):
                text = read(PACKAGING / name)
                self.assertIn("udevadm control --reload-rules", text)
                self.assertIn("update-desktop-database", text)
                self.assertIn("gtk-update-icon-cache", text)


class PackageBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = read(BUILD_DEB)

    def test_the_udev_rule_does_not_collide_with_greaseweazle_gui(self) -> None:
        """Two packages that own the same path cannot be installed together."""
        self.assertTrue((PACKAGING / "49-piratefinder-greaseweazle.rules").is_file())
        self.assertIn("/usr/lib/udev/rules.d/49-piratefinder-greaseweazle.rules", self.builder)
        self.assertNotIn("rules.d/49-greaseweazle.rules", self.builder)
        rules = read(PACKAGING / "49-piratefinder-greaseweazle.rules")
        self.assertIn('TAG+="uaccess"', rules)

    def test_the_package_installs_desktop_metadata_and_icons(self) -> None:
        for fragment in (
            "/usr/share/applications/${application_id}.desktop",
            "/usr/share/metainfo/${application_id}.metainfo.xml",
            "/usr/share/icons/hicolor/scalable/apps/${application_id}.svg",
            "/usr/share/icons/hicolor/symbolic/apps/${application_id}-symbolic.svg",
            "/usr/bin/piratefinder",
            "${application_lib}/bin/gw",
        ):
            self.assertIn(fragment, self.builder)
        self.assertIn(f'application_id="{APP_ID}"', self.builder)

    def test_the_catalogue_goes_where_the_application_looks(self) -> None:
        from piratefinder import paths

        self.assertEqual(paths.SYSTEM_CATALOGUE, Path("/usr/share/piratefinder/catalogue.sqlite"))
        self.assertIn('"${package_root}/usr/share/piratefinder/catalogue.sqlite"', self.builder)
        self.assertIn('catalogue="${project_dir}/build/catalogue.sqlite"', self.builder)
        self.assertIn("--no-catalogue", self.builder)

    def test_the_greaseweazle_source_is_pinned_and_verified(self) -> None:
        version = read(PACKAGING / "greaseweazle-version.txt").strip()
        digest = read(PACKAGING / "greaseweazle-source.sha256").strip()
        self.assertRegex(version, r"^\d+\.\d+$")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertIn("sha256sum --check", self.builder)
        for line in read(PACKAGING / "runtime-requirements.txt").splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==\S+$")

    def test_the_control_file_names_the_runtime_dependencies(self) -> None:
        for dependency in (
            "python3 (>= 3.12), python3 (<< 3.13)",
            "python3-gi",
            "gir1.2-gtk-4.0",
            "gir1.2-adw-1",
        ):
            self.assertIn(dependency, self.builder)
        self.assertIn("Recommends: 7zip", self.builder)
        self.assertIn(f"Homepage: {branding.HOMEPAGE}", self.builder)
        self.assertIn(
            "PirateFinder_${package_version}_ubuntu24.04_${architecture}.deb", self.builder
        )

    def test_documents_installed_by_the_package_exist(self) -> None:
        documents = re.search(r"for document in ([^;]+);", self.builder)
        self.assertIsNotNone(documents)
        for name in documents.group(1).split():
            with self.subTest(document=name):
                self.assertTrue((ROOT / name).is_file())


class VersionTests(unittest.TestCase):
    """The version is written once, in ``piratefinder/__init__.py``."""

    def test_pyproject_takes_the_version_from_the_package(self) -> None:
        pyproject = tomllib.loads(read(ROOT / "pyproject.toml"))
        self.assertNotIn("version", pyproject["project"])
        self.assertIn("version", pyproject["project"]["dynamic"])
        attr = pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        self.assertEqual(attr, "piratefinder.__version__")

    def test_the_package_states_a_version(self) -> None:
        init = runpy.run_path(str(ROOT / "src" / "piratefinder" / "__init__.py"))
        self.assertRegex(init["__version__"], r"^\d+\.\d+\.\d+$")

    def test_packaging_scripts_read_the_package_version(self) -> None:
        for script in (BUILD_DEB, PACKAGING / "check-release-tag.sh"):
            with self.subTest(script=script.name):
                text = read(script)
                self.assertIn("src/piratefinder/__init__.py", text)
                self.assertNotRegex(text, r'version="?\d+\.\d+\.\d+')


class WorkflowTests(unittest.TestCase):
    def test_the_release_workflow_verifies_and_smoke_tests_the_package(self) -> None:
        workflow = read(ROOT / ".github" / "workflows" / "release.yml")
        for required in (
            "./packaging/check-release-tag.sh",
            "catalogue-",
            "sha256sum --check",
            "/usr/lib/piratefinder/bin/gw info --help",
            "SHA256SUMS",
            "--verify-tag",
        ):
            self.assertIn(required, workflow)

    def test_catalogue_releases_are_never_marked_latest(self) -> None:
        workflow = read(ROOT / ".github" / "workflows" / "catalogue.yml")
        self.assertIn("--latest=false", workflow)
        self.assertNotIn("--prerelease", workflow)
        self.assertIn("catalogue.sqlite.gz.sha256", workflow)
        self.assertIn('tag="catalogue-$(date --utc +%F)"', workflow)
        self.assertIn("actions/cache@", workflow)


if __name__ == "__main__":
    unittest.main()
