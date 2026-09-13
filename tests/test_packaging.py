"""The desktop metadata, launcher and package builder agree with each other.

These files are read by different programs (the desktop shell, AppStream,
dpkg, udev, the shell running the launcher) and nothing at run time checks
that they describe the same application. A renamed icon or a launcher that
loses its PATH line produces a package that installs cleanly and then fails.
"""

from __future__ import annotations

import configparser
import glob
import re
import runpy
import subprocess
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from piratefinder import __version__, branding

ROOT = Path(__file__).resolve().parents[1]
APP_ID = branding.APPLICATION_ID
DESKTOP_FILE = ROOT / "data" / f"{APP_ID}.desktop"
METAINFO_FILE = ROOT / "data" / f"{APP_ID}.metainfo.xml"
PACKAGE = ROOT / "src" / "piratefinder"
ICONS = PACKAGE / "data" / "icons" / "hicolor"
COLOUR_ICON = ICONS / "scalable" / "apps" / f"{APP_ID}.svg"
SYMBOLIC_ICON = ICONS / "symbolic" / "apps" / f"{APP_ID}-symbolic.svg"
PACKAGING = ROOT / "packaging"
BUILD_DEB = PACKAGING / "build-deb.sh"
INSTALL_TEST = PACKAGING / "install-test.sh"
TARGETS = PACKAGING / "targets.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
SVG = "{http://www.w3.org/2000/svg}"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def target_table() -> dict[str, tuple[str, str, list[str]]]:
    """The rows of packaging/targets.sh: distribution to image, Python and architectures."""
    table = re.search(r"^target_table='\n(.*?)^'$", read(TARGETS), re.MULTILINE | re.DOTALL)
    assert table is not None, "packaging/targets.sh has no target_table"
    rows = {}
    for line in table.group(1).splitlines():
        distro, image, python, *architectures = line.split()
        rows[distro] = (image, python, architectures)
    return rows


def run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", *arguments], capture_output=True, text=True, timeout=60, check=False
    )


def running_target() -> tuple[str, str]:
    """This machine's distribution token and architecture, as the scripts see them."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            '. /etc/os-release && echo "${ID}-${VERSION_ID}" && '
            "(dpkg --print-architecture 2>/dev/null || echo none)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    distro, arch = (result.stdout.split() + ["none", "none"])[:2]
    return distro, arch


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


class PackageDataTests(unittest.TestCase):
    def test_every_data_file_is_package_data(self) -> None:
        """Icons, help pictures and virus tables are loaded from the installed package."""
        tool = tomllib.loads(read(ROOT / "pyproject.toml"))["tool"]
        patterns = tool["setuptools"]["package-data"]["piratefinder"]
        # setuptools expands each pattern with glob, relative to the package.
        matched = {
            name
            for pattern in patterns
            for name in glob.glob(pattern, root_dir=PACKAGE, recursive=True)
        }
        data = sorted(
            path.relative_to(PACKAGE).as_posix()
            for path in (PACKAGE / "data").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        self.assertTrue(data)
        self.assertEqual([name for name in data if name not in matched], [])


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

    def test_the_package_names_its_system_for_the_application_update(self) -> None:
        from piratefinder import app_update

        # The application reads the file beside itself, /usr/lib/piratefinder.
        self.assertEqual(app_update.PACKAGE_TARGET.name, "package-target")
        self.assertIn('application_lib="${package_root}/usr/lib/piratefinder"', self.builder)
        self.assertIn(
            'printf \'distro=%s\\narch=%s\\n\' "${distro}" "${arch}" '
            '> "${application_lib}/package-target"',
            self.builder,
        )
        self.assertIn("app_update.installed_target()", read(INSTALL_TEST))
        # The update looks for the package the build names.
        self.assertIn(
            'artifact_name="PirateFinder_${package_version}_${distro}_${arch}.deb"', self.builder
        )
        self.assertEqual(
            app_update.PackageTarget("debian-13", "armhf").package_name("1.2.3"),
            "PirateFinder_1.2.3_debian-13_armhf.deb",
        )

    def test_the_greaseweazle_source_is_pinned_and_verified(self) -> None:
        version = read(PACKAGING / "greaseweazle-version.txt").strip()
        digest = read(PACKAGING / "greaseweazle-source.sha256").strip()
        self.assertRegex(version, r"^\d+\.\d+$")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertIn("sha256sum --check", self.builder)
        for line in read(PACKAGING / "runtime-requirements.txt").splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==\S+$")

    def test_the_example_device_reports_the_bundled_host_tools(self) -> None:
        from piratefinder.ui.fake_backend import HOST_TOOLS, FakeBackend

        version = read(PACKAGING / "greaseweazle-version.txt").strip()
        self.assertEqual(HOST_TOOLS, version)
        self.assertEqual(FakeBackend().probe().host_tools, version)

    def test_the_control_file_names_the_runtime_dependencies(self) -> None:
        depends = re.search(r"^Depends: (.*)$", self.builder, re.MULTILINE)
        self.assertIsNotNone(depends)
        self.assertEqual(
            depends.group(1),
            "python3 (>= ${target_python}), python3 (<< ${target_python_next}), "
            "python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5)",
        )
        self.assertIn("Architecture: ${arch}", self.builder)
        self.assertIn("Recommends: 7zip", self.builder)
        self.assertIn(f"Homepage: {branding.HOMEPAGE}", self.builder)
        self.assertIn(
            'artifact_name="PirateFinder_${package_version}_${distro}_${arch}.deb"', self.builder
        )

    def test_compiled_modules_are_checked_where_they_are_built_and_installed(self) -> None:
        modules = "bitarray._bitarray, crcmod._crcfunext, greaseweazle.optimised.optimised"
        self.assertIn(modules, self.builder)
        self.assertIn(modules, read(INSTALL_TEST))

    def test_documents_installed_by_the_package_exist(self) -> None:
        documents = re.search(r"for document in ([^;]+);", self.builder)
        self.assertIsNotNone(documents)
        for name in documents.group(1).split():
            with self.subTest(document=name):
                self.assertTrue((ROOT / name).is_file())


class TargetTests(unittest.TestCase):
    """packaging/targets.sh is the one list of distribution releases and architectures."""

    def test_the_releases_python_versions_and_architectures(self) -> None:
        self.assertEqual(
            target_table(),
            {
                "ubuntu-24.04": ("ubuntu:24.04", "3.12", ["amd64", "arm64", "armhf"]),
                "debian-13": ("debian:trixie", "3.13", ["amd64", "arm64", "armhf"]),
            },
        )

    @staticmethod
    def resolve(distro: str, arch: str) -> subprocess.CompletedProcess[str]:
        script = (
            'source "$1" && resolve_target "$2" "$3" && echo "${target_image}" '
            '"${target_python}" "${target_python_next}" "${target_platform}"'
        )
        return run_script("-c", script, "bash", str(TARGETS), distro, arch)

    def test_each_target_resolves_to_an_image_python_range_and_platform(self) -> None:
        platforms = {"amd64": "linux/amd64", "arm64": "linux/arm64", "armhf": "linux/arm/v7"}
        for distro, (image, python, architectures) in target_table().items():
            major, minor = python.split(".")
            for arch in architectures:
                with self.subTest(distro=distro, arch=arch):
                    result = self.resolve(distro, arch)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout.split(),
                        [image, python, f"{major}.{int(minor) + 1}", platforms[arch]],
                    )

    def test_an_unknown_target_is_refused(self) -> None:
        for distro, arch in (("fedora-40", "amd64"), ("debian-13", "i386"), ("", "")):
            with self.subTest(distro=distro, arch=arch):
                result = self.resolve(distro, arch)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("--distro ubuntu-24.04 --arch amd64|arm64|armhf", result.stderr)

    def test_the_builder_needs_a_supported_distribution(self) -> None:
        result = run_script(str(BUILD_DEB), "--no-catalogue")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--distro", result.stderr)
        result = run_script(str(BUILD_DEB), "--distro", "ubuntu-22.04", "--arch", "amd64")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unsupported target ubuntu-22.04 amd64", result.stderr)
        result = run_script(str(BUILD_DEB), "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--distro DISTRO [--arch ARCH] [--container]", result.stderr)

    def test_a_native_build_refuses_another_release_or_architecture(self) -> None:
        """Without --container the build stops before it downloads or writes anything."""
        here = running_target()
        distro, arch = next(
            (distro, arch)
            for distro, (_image, _python, architectures) in target_table().items()
            for arch in architectures
            if (distro, arch) != here
        )
        with tempfile.TemporaryDirectory() as output:
            result = run_script(
                str(BUILD_DEB), "--distro", distro, f"--arch={arch}", "--no-catalogue", output
            )
            self.assertEqual(list(Path(output).iterdir()), [])
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"not {distro} {arch}", result.stderr)
        self.assertIn("--container", result.stderr)

    def test_the_install_test_takes_the_target_from_the_file_name(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for name, message in (
                ("piratefinder_0.1.0_amd64.deb", "is not named"),
                (f"PirateFinder_{__version__}_fedora-40_amd64.deb", "Unsupported target"),
                (f"PirateFinder_{__version__}_debian-13_riscv64.deb", "Unsupported target"),
            ):
                with self.subTest(name=name):
                    package = Path(folder) / name
                    package.touch()
                    result = run_script(str(INSTALL_TEST), str(package))
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(message, result.stderr)
        text = read(INSTALL_TEST)
        self.assertIn(r"^PirateFinder_([^_]+)_([^_]+)_([^_]+)\.deb$", text)
        self.assertIn('apt-get install -y -qq "${package}"', text)
        self.assertIn("/usr/lib/piratefinder/bin/gw info --help", text)
        self.assertIn("xvfb-run", text)


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
        workflow = read(WORKFLOWS / "release.yml")
        for required in (
            "./packaging/check-release-tag.sh",
            "catalogue-",
            "sha256sum --check",
            "docker/setup-qemu-action@",
            './packaging/build-deb.sh --container --distro "${DISTRO}" --arch "${ARCH}"',
            "--catalogue build/catalogue.sqlite dist",
            "./packaging/install-test.sh --require-catalogue dist/*.deb",
            "sha256sum -- *.deb > SHA256SUMS",
            "--verify-tag",
            "needs.catalogue.outputs.tag",
        ):
            self.assertIn(required, workflow)

    def test_the_release_matrix_builds_every_target(self) -> None:
        workflow = read(WORKFLOWS / "release.yml")
        distros = re.search(r"^\s+distro: \[(.*)\]$", workflow, re.MULTILINE)
        arches = re.search(r"^\s+arch: \[(.*)\]$", workflow, re.MULTILINE)
        self.assertIsNotNone(distros)
        self.assertIsNotNone(arches)
        table = target_table()
        self.assertEqual([name.strip() for name in distros.group(1).split(",")], list(table))
        for distro, (_image, _python, architectures) in table.items():
            with self.subTest(distro=distro):
                self.assertEqual(
                    [name.strip() for name in arches.group(1).split(",")], architectures
                )
        for arch in ("amd64", "arm64", "armhf"):
            self.assertRegex(workflow, rf"- arch: {arch}\n\s+runner: ubuntu-24\.04")

    def test_the_ci_packages_are_release_targets(self) -> None:
        workflow = read(WORKFLOWS / "ci.yml")
        builds = re.findall(r"- distro: (\S+)\n\s+arch: (\S+)\n", workflow)
        self.assertIn(("ubuntu-24.04", "amd64"), builds)
        table = target_table()
        for distro, arch in builds:
            with self.subTest(distro=distro, arch=arch):
                self.assertIn(arch, table[distro][2])
        self.assertIn("./packaging/build-deb.sh --container", workflow)
        self.assertIn("./packaging/install-test.sh dist/*.deb", workflow)

    def test_every_script_a_workflow_runs_exists(self) -> None:
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            for script in re.findall(r"\./((?:packaging|tools)/[\w.-]+)", read(workflow)):
                with self.subTest(workflow=workflow.name, script=script):
                    self.assertTrue((ROOT / script).is_file())

    def test_catalogue_releases_are_never_marked_latest(self) -> None:
        workflow = read(ROOT / ".github" / "workflows" / "catalogue.yml")
        self.assertIn("--latest=false", workflow)
        self.assertNotIn("--prerelease", workflow)
        self.assertIn('tag="catalogue-$(date --utc +%F)"', workflow)
        self.assertIn("actions/cache@", workflow)

    def test_application_releases_are_the_latest_with_checksums_for_the_update(self) -> None:
        from piratefinder import app_update

        workflow = read(ROOT / ".github" / "workflows" / "release.yml")
        # The update reads the release marked latest and checks the package
        # against SHA256SUMS.
        self.assertIn("--latest", workflow)
        self.assertIn(f"sha256sum -- *.deb > {app_update.SUMS_NAME}", workflow)
        self.assertTrue(app_update.LATEST_URL.endswith("/releases/latest"))

    def test_both_workflows_name_the_catalogue_by_the_layout_the_application_reads(self) -> None:
        from piratefinder.catalogue.schema import SCHEMA_VERSION
        from piratefinder.catalogue.update import asset_name

        layout = (
            "layout=\"$(python3 -c 'from piratefinder.catalogue.schema import SCHEMA_VERSION; "
            "print(SCHEMA_VERSION)')\""
        )
        pattern = 'asset="catalogue-layout${layout}.sqlite.gz"'
        self.assertEqual(
            pattern.split('"')[1].replace("${layout}", str(SCHEMA_VERSION)), asset_name()
        )
        for name in ("catalogue.yml", "release.yml"):
            workflow = read(ROOT / ".github" / "workflows" / name)
            with self.subTest(workflow=name):
                self.assertIn(layout, workflow)
                self.assertIn(pattern, workflow)
                self.assertIn('"${asset}.sha256"', workflow)
                self.assertNotIn("--pattern catalogue.sqlite.gz", workflow)
        release = read(ROOT / ".github" / "workflows" / "release.yml")
        self.assertIn('select(any(.assets[]; .name == \\"${asset}\\"))', release)


if __name__ == "__main__":
    unittest.main()
