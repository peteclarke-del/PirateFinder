# shellcheck shell=bash
# The target_ variables are set here for the scripts that source this file.
# shellcheck disable=SC2034
#
# The distribution releases and architectures PirateFinder is packaged for.
# build-deb.sh and install-test.sh source this file; nothing else holds the
# table. The release workflow's matrix lists the same distributions and
# architectures, and tests/test_packaging.py checks that it does.
#
# Each row names a distribution token, the Docker image of that release, the
# Python minor version the release ships and the Debian architectures built for
# it. The token is the release's ID and VERSION_ID from /etc/os-release, and it
# appears in the package file name. The bundled Greaseweazle and its native
# dependencies are compiled for that one Python, so the package depends on
# python3 from that minor version up to, but not including, the next.

# Distribution  Docker image   Python  Architectures
target_table='
ubuntu-24.04    ubuntu:24.04   3.12    amd64 arm64 armhf
debian-13       debian:trixie  3.13    amd64 arm64 armhf
'

# resolve_target DISTRO ARCH
#
# Sets target_image, target_python, target_python_next and target_platform for
# one row of the table, or prints the supported targets and returns 1.
resolve_target() {
    local want_distro="$1" want_arch="$2"
    local distro image python architectures
    while read -r distro image python architectures; do
        if [[ -n "${distro}" && "${distro}" == "${want_distro}" \
            && -n "${want_arch}" && " ${architectures} " == *" ${want_arch} "* ]]; then
            target_image="${image}"
            target_python="${python}"
            target_python_next="${python%%.*}.$((${python#*.} + 1))"
            case "${want_arch}" in
                amd64) target_platform=linux/amd64 ;;
                arm64) target_platform=linux/arm64 ;;
                armhf) target_platform=linux/arm/v7 ;;
                *)
                    echo "No Docker platform is known for ${want_arch}." >&2
                    return 1
                    ;;
            esac
            return 0
        fi
    done <<< "${target_table}"
    echo "Unsupported target ${want_distro:-<none>} ${want_arch:-<none>}. Supported targets:" >&2
    while read -r distro image python architectures; do
        if [[ -n "${distro}" ]]; then
            echo "  --distro ${distro} --arch ${architectures// /|}" >&2
        fi
    done <<< "${target_table}"
    return 1
}

# pull_image IMAGE PLATFORM
#
# Pulls IMAGE for PLATFORM and prints its ID, which the caller runs rather
# than the tag: Docker's classic image store keeps one platform per tag, so
# another build pulling the same tag for another platform moves the tag.
# Docker Hub refuses or resets pulls often enough to fail a build that has
# nothing wrong with it, so the pull is retried.
pull_image() {
    local attempt
    for attempt in 1 2 3 4; do
        if docker pull --quiet --platform "$2" "$1" >/dev/null; then
            docker image inspect --format '{{.Id}}' "$1"
            return 0
        fi
        echo "Pulling $1 for $2 failed on attempt ${attempt}." >&2
        sleep $((attempt * 15))
    done
    return 1
}
