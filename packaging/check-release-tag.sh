#!/usr/bin/env bash

set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_version="$(cd "${project_dir}" && python3 -c \
    'import runpy; print(runpy.run_path("src/piratefinder/__init__.py")["__version__"])')"
dynamic_version="$(cd "${project_dir}" && python3 -c \
    'import tomllib; p = tomllib.load(open("pyproject.toml", "rb")); print(p["tool"]["setuptools"]["dynamic"]["version"]["attr"])')"
release_tag="${1:-}"

# pyproject.toml must not state a version of its own; it reads __version__.
if [[ "${dynamic_version}" != "piratefinder.__version__" ]]; then
    echo "pyproject.toml must take its version from piratefinder.__version__, not ${dynamic_version}." >&2
    exit 1
fi
if [[ "${release_tag}" != "v${runtime_version}" ]]; then
    echo "Release tag ${release_tag:-<missing>} does not match project version v${runtime_version}." >&2
    exit 1
fi

echo "Release tag ${release_tag} matches the project version ${runtime_version}."
