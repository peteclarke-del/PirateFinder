#!/usr/bin/env bash

# Install a built PirateFinder package on the distribution release and
# architecture it was built for, and check that it works there.
#
# Usage: packaging/install-test.sh [--require-catalogue] [--here] PACKAGE.deb
#
# The release and architecture are read from the file name,
# PirateFinder_<version>_<distro>_<arch>.deb, and must be a pair in the table
# in packaging/targets.sh. The script runs itself with --here in a new Docker
# container of that release and architecture; Docker runs other architectures
# through qemu binfmt handlers. --here runs the checks on this machine instead,
# which must be that release and architecture, as root; it installs packages.
# --require-catalogue fails unless the package ships the catalogue.
#
# The checks: the package's name, version and architecture fields; no file
# under /usr writable by group or others; apt installs it with its
# dependencies; the bundled gw runs; every compiled module of the bundled
# Greaseweazle loads; the packaged catalogue opens; and the application opens
# its window under Xvfb.

set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
require_catalogue=0
here=0
package=""

# shellcheck source=packaging/targets.sh
source "${project_dir}/packaging/targets.sh"

usage() {
    echo "Usage: $0 [--require-catalogue] [--here] PACKAGE.deb" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --require-catalogue)
            require_catalogue=1
            shift
            ;;
        --here)
            here=1
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
            if [[ -n "${package}" ]]; then
                usage
                exit 2
            fi
            package="$1"
            shift
            ;;
    esac
done
if [[ -z "${package}" || ! -f "${package}" ]]; then
    echo "No package file: ${package:-<none>}" >&2
    usage
    exit 2
fi

package_name="$(basename -- "${package}")"
package_dir="$(cd -- "$(dirname -- "${package}")" && pwd)"
# apt installs a file only when it is given as a path.
package="${package_dir}/${package_name}"
if [[ ! "${package_name}" =~ ^PirateFinder_([^_]+)_([^_]+)_([^_]+)\.deb$ ]]; then
    echo "${package_name} is not named PirateFinder_<version>_<distro>_<arch>.deb." >&2
    exit 2
fi
version="${BASH_REMATCH[1]}"
distro="${BASH_REMATCH[2]}"
arch="${BASH_REMATCH[3]}"
resolve_target "${distro}" "${arch}" || exit 2

if [[ "${here}" == 0 ]]; then
    arguments=(--here)
    if [[ "${require_catalogue}" == 1 ]]; then
        arguments+=(--require-catalogue)
    fi
    image_id="$(pull_image "${target_image}" "${target_platform}")"
    exec docker run --rm \
        --platform "${target_platform}" \
        --env DEBIAN_FRONTEND=noninteractive \
        --volume "${project_dir}:/source:ro" \
        --volume "${package_dir}:/packages:ro" \
        "${image_id}" \
        /source/packaging/install-test.sh "${arguments[@]}" "/packages/${package_name}"
fi

# From here on the checks run on the target system.
export PYTHONDONTWRITEBYTECODE=1
# shellcheck source=/dev/null
running_distro="$(. /etc/os-release && echo "${ID}-${VERSION_ID}")"
running_arch="$(dpkg --print-architecture)"
if [[ "${running_distro}" != "${distro}" || "${running_arch}" != "${arch}" ]]; then
    echo "This machine is ${running_distro} ${running_arch}, not ${distro} ${arch}." >&2
    exit 1
fi

echo "Checking ${package_name} on ${distro} ${arch}."
for field in Package:piratefinder "Version:${version}" "Architecture:${arch}"; do
    found="$(dpkg-deb --field "${package}" "${field%%:*}")"
    if [[ "${found}" != "${field#*:}" ]]; then
        echo "The package's ${field%%:*} field is ${found}, not ${field#*:}." >&2
        exit 1
    fi
done
dpkg-deb --field "${package}" Depends

package_root="$(mktemp -d)"
log="$(mktemp)"
trap 'rm -rf -- "${package_root}" "${log}"' EXIT
dpkg-deb --extract "${package}" "${package_root}"
if find "${package_root}/usr" -perm /022 -print -quit | grep -q .; then
    echo "The package contains group-writable or world-writable files." >&2
    exit 1
fi
if [[ "${require_catalogue}" == 1 && ! -f "${package_root}/usr/share/piratefinder/catalogue.sqlite" ]]; then
    echo "The package does not contain the catalogue." >&2
    exit 1
fi

# apt installs the recommended packages as well, as it does for a user.
apt-get update -qq
apt-get install -y -qq "${package}" > /dev/null
test -x /usr/bin/piratefinder
test -f /usr/lib/udev/rules.d/49-piratefinder-greaseweazle.rules
/usr/lib/piratefinder/bin/gw info --help
PYTHONPATH=/usr/lib/piratefinder /usr/bin/python3 -c "
import bitarray._bitarray, crcmod._crcfunext, greaseweazle.optimised.optimised
import gi
gi.require_version('Adw', '1')
from gi.repository import Adw
import greaseweazle, piratefinder
from piratefinder import app_update
print('PirateFinder', piratefinder.__version__, 'with Greaseweazle', greaseweazle.__version__,
      'and libadwaita', f'{Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}.{Adw.MICRO_VERSION}')
target = app_update.installed_target()
if target != app_update.PackageTarget('${distro}', '${arch}'):
    raise SystemExit(f'The package says it was built for {target}, not ${distro} ${arch}.')
"
if [[ -f /usr/share/piratefinder/catalogue.sqlite ]]; then
    PYTHONPATH=/usr/lib/piratefinder /usr/bin/python3 -c "
from pathlib import Path
from piratefinder.catalogue.store import Catalogue
print(Catalogue.open(Path('/usr/share/piratefinder/catalogue.sqlite')).stats())
"
fi

# Start the application on a virtual display and wait for its window. GTK
# names a 1x1 leader window after the application as soon as it opens the
# display, so only a larger window counts. The first start under emulation can
# take minutes.
apt-get install -y -qq --no-install-recommends xvfb xauth x11-utils dbus-daemon > /dev/null
# shellcheck disable=SC2016 # expanded by the shell under Xvfb
if ! GSK_RENDERER=cairo dbus-run-session -- xvfb-run --auto-servernum bash -c '
    piratefinder > "$1" 2>&1 &
    application=$!
    for _ in $(seq 1 300); do
        if xwininfo -root -tree | grep "\"PirateFinder\"" | grep -q -v " 1x1+"; then
            kill "${application}"
            exit 0
        fi
        if ! kill -0 "${application}" 2> /dev/null; then
            echo "The application exited before it opened its window." >&2
            exit 1
        fi
        sleep 1
    done
    echo "The application opened no window in 300 seconds." >&2
    exit 1
' bash "${log}"; then
    cat "${log}" >&2
    exit 1
fi
if grep -q Traceback "${log}"; then
    cat "${log}" >&2
    exit 1
fi
echo "The PirateFinder window opened."
echo "${package_name} passed the installation test on ${distro} ${arch}."
