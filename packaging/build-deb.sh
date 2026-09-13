#!/usr/bin/env bash

# Build the PirateFinder Debian package for one distribution release and
# architecture.
#
# Usage: packaging/build-deb.sh --distro DISTRO [--arch ARCH] [--container]
#                               [--catalogue FILE | --no-catalogue] [OUTPUT_DIR]
#
# DISTRO is ubuntu-24.04 or debian-13 and ARCH is amd64, arm64 or armhf; the
# table in packaging/targets.sh lists the supported pairs. ARCH defaults to the
# architecture of the machine running the script. The package is written to
# OUTPUT_DIR (default dist) as PirateFinder_<version>_<distro>_<arch>.deb.
#
# Greaseweazle and some of its dependencies are compiled extensions, so the
# package is built on the release and architecture it is for. Without
# --container the build runs on this machine, which must be that release and
# architecture, with python3, pip, a C compiler and the Python headers
# installed. With --container the script runs itself in a Docker container of
# the target release and architecture and installs those there; Docker runs
# other architectures through qemu binfmt handlers.
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
container=0
distro=""
arch=""
output_dir=""

# shellcheck source=packaging/targets.sh
source "${project_dir}/packaging/targets.sh"

usage() {
    cat >&2 <<EOF
Usage: $0 --distro DISTRO [--arch ARCH] [--container]
       [--catalogue FILE | --no-catalogue] [OUTPUT_DIR]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-catalogue)
            include_catalogue=0
            shift
            ;;
        --container)
            container=1
            shift
            ;;
        --catalogue|--distro|--arch)
            if [[ $# -lt 2 ]]; then
                usage
                exit 2
            fi
            case "$1" in
                --catalogue) catalogue="$2" ;;
                --distro) distro="$2" ;;
                --arch) arch="$2" ;;
            esac
            shift 2
            ;;
        --catalogue=*)
            catalogue="${1#--catalogue=}"
            shift
            ;;
        --distro=*)
            distro="${1#--distro=}"
            shift
            ;;
        --arch=*)
            arch="${1#--arch=}"
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

if [[ -z "${distro}" ]]; then
    echo "Name the distribution release to build for with --distro." >&2
    usage
    exit 2
fi
arch="${arch:-$(dpkg --print-architecture)}"
resolve_target "${distro}" "${arch}" || exit 2

# pyproject.toml takes its version from the package, so __init__.py is the
# single source of the version number.
package_version="$(cd "${project_dir}" && "${python_command}" -c \
    'import runpy; print(runpy.run_path("src/piratefinder/__init__.py")["__version__"])')"
if [[ "${package_version}" == *-* ]]; then
    echo "The project version must not contain a Debian revision separator: ${package_version}" >&2
    exit 1
fi
artifact_name="PirateFinder_${package_version}_${distro}_${arch}.deb"

if [[ "${include_catalogue}" == 1 && ! -f "${catalogue}" ]]; then
    cat >&2 <<EOF
No catalogue found at ${catalogue}.
Build one with "PYTHONPATH=src python3 -m catalogue_builder", pass
--catalogue FILE to use another copy (for example a published
catalogue.sqlite.gz, decompressed), or pass --no-catalogue to build a package
without a catalogue.
EOF
    exit 1
fi

if [[ "${container}" == 1 ]]; then
    install -d "${output_dir}"
    output_dir="$(cd -- "${output_dir}" && pwd)"
    inner_arguments=(--distro "${distro}" --arch "${arch}" --no-catalogue)
    catalogue_mount=()
    if [[ "${include_catalogue}" == 1 ]]; then
        catalogue="$(cd -- "$(dirname -- "${catalogue}")" && pwd)/$(basename -- "${catalogue}")"
        inner_arguments=(--distro "${distro}" --arch "${arch}" --catalogue /catalogue.sqlite)
        catalogue_mount=(--volume "${catalogue}:/catalogue.sqlite:ro")
    fi
    image_id="$(pull_image "${target_image}" "${target_platform}")"
    # The source is mounted read-only; the build copies what it needs. The
    # package is written as root inside the container, then handed to the
    # user who ran this script.
    # shellcheck disable=SC2016 # expanded by the shell in the container
    docker run --rm \
        --platform "${target_platform}" \
        --env DEBIAN_FRONTEND=noninteractive \
        --env PIP_ROOT_USER_ACTION=ignore \
        --env OUTPUT_OWNER="$(id -u):$(id -g)" \
        --env ARTIFACT="/output/${artifact_name}" \
        --volume "${project_dir}:/source:ro" \
        --volume "${output_dir}:/output" \
        "${catalogue_mount[@]}" \
        "${image_id}" \
        bash -euo pipefail -c '
            apt-get update -qq
            apt-get install -y -qq --no-install-recommends \
                build-essential ca-certificates curl python3 python3-dev python3-pip \
                > /dev/null
            /source/packaging/build-deb.sh "$@"
            chown -- "${OUTPUT_OWNER}" "${ARTIFACT}"
        ' build-deb.sh "${inner_arguments[@]}" /output
    exit 0
fi

# A native build must run on the release and architecture it is for.
# shellcheck source=/dev/null
running_distro="$(. /etc/os-release && echo "${ID}-${VERSION_ID}")"
running_arch="$(dpkg --print-architecture)"
python_version="$("${python_command}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${running_distro}" != "${distro}" || "${running_arch}" != "${arch}" ]]; then
    echo "This machine is ${running_distro} ${running_arch}, not ${distro} ${arch}." >&2
    echo "Build on ${distro} ${arch}, or add --container." >&2
    exit 1
fi
if [[ "${python_version}" != "${target_python}" ]]; then
    echo "The ${distro} package must be built with Python ${target_python}, not ${python_version}." >&2
    exit 1
fi

greaseweazle_version="$(tr -d '[:space:]' < "${project_dir}/packaging/greaseweazle-version.txt")"
greaseweazle_sha256="$(tr -d '[:space:]' < "${project_dir}/packaging/greaseweazle-source.sha256")"

if [[ "${include_catalogue}" == 1 ]]; then
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
package_root="${build_dir}/piratefinder_${package_version}_${arch}"
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

# Greaseweazle's own extension and crcmod's quietly fall back to slow or
# missing code when they fail to build, so the build stops unless every
# compiled module loads in the Python the package is for.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${application_lib}" "${python_command}" -c \
    'import bitarray._bitarray, crcmod._crcfunext, greaseweazle.optimised.optimised'

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

# libadwaita 1.5 is the oldest the interface supports (docs/DESIGN.md).
installed_size="$(du -sk "${package_root}/usr" | cut -f1)"
cat > "${package_root}/DEBIAN/control" <<EOF
Package: piratefinder
Version: ${package_version}
Section: utils
Priority: optional
Architecture: ${arch}
Installed-Size: ${installed_size}
Maintainer: Pete Clarke <peteclarke-del@users.noreply.github.com>
Depends: python3 (>= ${target_python}), python3 (<< ${target_python_next}), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5)
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

artifact="${output_dir}/${artifact_name}"
dpkg-deb --root-owner-group --build "${package_root}" "${artifact}"
echo "Created ${artifact}"
