#!/usr/bin/env bash

# Build the Ubuntu 24.04 package for PirateFinder.
#
# Usage: packaging/build-deb.sh [--catalogue FILE | --no-catalogue] [OUTPUT_DIR]
#
# The catalogue is taken from build/catalogue.sqlite unless --catalogue names
# another file. --no-catalogue builds a package without one, for testing the
# packaging itself; the application then relies on a catalogue downloaded by
# its own update check.

set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_command="${PYTHON:-python3}"
catalogue="${project_dir}/build/catalogue.sqlite"
include_catalogue=1
output_dir=""

usage() {
    echo "Usage: $0 [--catalogue FILE | --no-catalogue] [OUTPUT_DIR]" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-catalogue)
            include_catalogue=0
            shift
            ;;
        --catalogue)
            if [[ $# -lt 2 ]]; then
                usage
                exit 2
            fi
            catalogue="$2"
            shift 2
            ;;
        --catalogue=*)
            catalogue="${1#--catalogue=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage
            exit 2
            ;;
        *)
            if [[ -n "${output_dir}" ]]; then
                usage
                exit 2
            fi
            output_dir="$1"
            shift
            ;;
    esac
done
output_dir="${output_dir:-${project_dir}/dist}"

# pyproject.toml takes its version from the package, so __init__.py is the
# single source of the version number.
package_version="$(cd "${project_dir}" && "${python_command}" -c \
    'import runpy; print(runpy.run_path("src/piratefinder/__init__.py")["__version__"])')"
greaseweazle_version="$(tr -d '[:space:]' < "${project_dir}/packaging/greaseweazle-version.txt")"
greaseweazle_sha256="$(tr -d '[:space:]' < "${project_dir}/packaging/greaseweazle-source.sha256")"
python_version="$("${python_command}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
architecture="$(dpkg --print-architecture)"

if [[ "${python_version}" != "3.12" ]]; then
    echo "The Ubuntu 24.04 package must be built with Python 3.12, not ${python_version}." >&2
    exit 1
fi
if [[ "${package_version}" == *-* ]]; then
    echo "The project version must not contain a Debian revision separator: ${package_version}" >&2
    exit 1
fi

if [[ "${include_catalogue}" == 1 ]]; then
    if [[ ! -f "${catalogue}" ]]; then
        cat >&2 <<EOF
No catalogue found at ${catalogue}.
Build one with "PYTHONPATH=src python3 -m catalogue_builder", pass
--catalogue FILE to use another copy (for example a published
catalogue.sqlite.gz, decompressed), or pass --no-catalogue to build a package
without a catalogue.
EOF
        exit 1
    fi
    # Refuse a file the application could not open: it must be SQLite, carry
    # the schema version this release reads, and hold at least one disk.
    "${python_command}" - "${catalogue}" "${project_dir}/src/piratefinder/catalogue/schema.py" <<'EOF'
import runpy
import sqlite3
import sys

path, schema_file = sys.argv[1], sys.argv[2]
expected = str(runpy.run_path(schema_file)["SCHEMA_VERSION"])
try:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    disks = connection.execute("SELECT COUNT(*) FROM disks").fetchone()[0]
    connection.close()
except sqlite3.Error as error:
    sys.exit(f"{path} is not a PirateFinder catalogue: {error}")
if row is None or row[0] != expected:
    found = row[0] if row else "none"
    sys.exit(f"{path} has catalogue schema {found}; this release reads schema {expected}.")
if disks == 0:
    sys.exit(f"{path} contains no disks.")
print(f"Catalogue {path}: schema {expected}, {disks} disks.")
EOF
fi

build_dir="$(mktemp -d)"
trap 'rm -rf -- "${build_dir}"' EXIT
greaseweazle_archive="${build_dir}/greaseweazle-v${greaseweazle_version}.tar.gz"
source_copy="${build_dir}/source"
package_root="${build_dir}/piratefinder_${package_version}_${architecture}"
application_lib="${package_root}/usr/lib/piratefinder"
doc_dir="${package_root}/usr/share/doc/piratefinder"
icon_source="${project_dir}/src/piratefinder/data/icons/hicolor"
application_id="com.github.pclarke.PirateFinder"

curl --fail --location --silent --show-error \
    --output "${greaseweazle_archive}" \
    "https://github.com/keirf/greaseweazle/archive/refs/tags/v${greaseweazle_version}.tar.gz"
echo "${greaseweazle_sha256}  ${greaseweazle_archive}" | sha256sum --check --status

# Build the project from a clean copy so that nothing in the working tree
# (build output, caches, a local catalogue) reaches the package, and so the
# build leaves no egg-info or build directory behind in the checkout.
install -d "${source_copy}"
tar -C "${project_dir}" \
    --exclude='__pycache__' --exclude='*.py[co]' --exclude='*.egg-info' \
    -cf - pyproject.toml README.md LICENSE src \
    | tar -C "${source_copy}" -xf -

install -d \
    "${application_lib}" \
    "${package_root}/usr/bin" \
    "${package_root}/usr/share/applications" \
    "${package_root}/usr/share/metainfo" \
    "${package_root}/usr/share/icons/hicolor/scalable/apps" \
    "${package_root}/usr/share/icons/hicolor/symbolic/apps" \
    "${package_root}/usr/lib/udev/rules.d" \
    "${doc_dir}" \
    "${package_root}/DEBIAN" \
    "${output_dir}"

# One pip run installs the pinned Greaseweazle dependencies, PirateFinder and
# Greaseweazle into the private library. The source archive has no Git
# metadata, so setuptools-scm is told the version. PirateFinder does not use
# setuptools-scm, so the general variable does not affect it.
SETUPTOOLS_SCM_PRETEND_VERSION="${greaseweazle_version}" \
SETUPTOOLS_SCM_PRETEND_VERSION_FOR_GREASEWEAZLE="${greaseweazle_version}" \
"${python_command}" -m pip install \
    --disable-pip-version-check \
    --no-compile \
    --no-deps \
    --ignore-installed \
    --target "${application_lib}" \
    --requirement "${project_dir}/packaging/runtime-requirements.txt" \
    "${source_copy}" \
    "${greaseweazle_archive}"

# Wheels may preserve a cooperative build umask. Installed application files
# must never remain group writable under /usr. Bytecode from the build machine
# is not shipped; Python writes none under /usr at run time either.
chmod -R go-w "${application_lib}"
find "${application_lib}" -name __pycache__ -prune -exec rm -rf -- {} +
find "${application_lib}" -name '*.py[co]' -delete
rm -rf -- "${application_lib:?}/bin"
install -d "${application_lib}/bin"
install -m 0755 "${project_dir}/packaging/gw" "${application_lib}/bin/gw"
install -m 0644 "${project_dir}/packaging/gw_entry.py" "${application_lib}/gw_entry.py"
install -m 0755 "${project_dir}/packaging/piratefinder" "${package_root}/usr/bin/piratefinder"
install -m 0644 "${project_dir}/data/${application_id}.desktop" \
    "${package_root}/usr/share/applications/${application_id}.desktop"
install -m 0644 "${project_dir}/data/${application_id}.metainfo.xml" \
    "${package_root}/usr/share/metainfo/${application_id}.metainfo.xml"
install -m 0644 "${icon_source}/scalable/apps/${application_id}.svg" \
    "${package_root}/usr/share/icons/hicolor/scalable/apps/${application_id}.svg"
install -m 0644 "${icon_source}/symbolic/apps/${application_id}-symbolic.svg" \
    "${package_root}/usr/share/icons/hicolor/symbolic/apps/${application_id}-symbolic.svg"
# Greaseweazle-GUI ships the same rules as 49-greaseweazle.rules. A different
# file name lets both packages be installed together.
install -m 0644 "${project_dir}/packaging/49-piratefinder-greaseweazle.rules" \
    "${package_root}/usr/lib/udev/rules.d/49-piratefinder-greaseweazle.rules"
if [[ "${include_catalogue}" == 1 ]]; then
    install -d "${package_root}/usr/share/piratefinder"
    install -m 0644 "${catalogue}" "${package_root}/usr/share/piratefinder/catalogue.sqlite"
fi
for document in README.md SECURITY.md THIRD_PARTY_NOTICES.md NOTICE docs/DATA_SOURCES.md; do
    install -m 0644 "${project_dir}/${document}" "${doc_dir}/$(basename -- "${document}")"
done
tar -xOf "${greaseweazle_archive}" \
    "greaseweazle-${greaseweazle_version}/COPYING" \
    > "${doc_dir}/COPYING.greaseweazle"
chmod 0644 "${doc_dir}/COPYING.greaseweazle"
install -m 0755 "${project_dir}/packaging/postinst" "${package_root}/DEBIAN/postinst"
install -m 0755 "${project_dir}/packaging/postrm" "${package_root}/DEBIAN/postrm"

installed_size="$(du -sk "${package_root}/usr" | cut -f1)"
cat > "${package_root}/DEBIAN/control" <<EOF
Package: piratefinder
Version: ${package_version}
Section: utils
Priority: optional
Architecture: ${architecture}
Installed-Size: ${installed_size}
Maintainer: Pete Clarke <peteclarke-del@users.noreply.github.com>
Depends: python3 (>= 3.12), python3 (<< 3.13), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1
Recommends: 7zip
Homepage: https://github.com/peteclarke-del/PirateFinder
Description: Find Amiga and Atari ST menu disks and write them to floppy
 PirateFinder keeps a searchable catalogue of what is on Amiga and Atari ST
 menu disks, compacts, packs and single cracks. It finds the disk images in
 local or network folders, or downloads them on request from online
 archives, and writes them to floppy with a Greaseweazle. The package
 includes Greaseweazle Host Tools ${greaseweazle_version} and the Linux
 device-access rules.
EOF

artifact="${output_dir}/PirateFinder_${package_version}_ubuntu24.04_${architecture}.deb"
dpkg-deb --root-owner-group --build "${package_root}" "${artifact}"
echo "Created ${artifact}"
