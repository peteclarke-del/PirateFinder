"""Build catalogue.sqlite from the public sources.

    python3 -m catalogue_builder [--cache DIR] [--output FILE] [--offline]
        [--only a,b] [--skip a,b] [--with a,b] [--input id=path ...]
        [--list-sources] [--strict]

Every module in ``catalogue_builder/sources`` that defines ``INFO`` and
``collect`` is a source. A module may also set ``CONTENT_PRIORITY`` (lower
wins when several sources list a disk's contents, default 90) and
``DEFAULT_ENABLED`` (False for heavy optional sources, which then run only
when named with ``--with`` or ``--only``), and define
``collect_crews(ctx)`` returning ``CrewRecord``s for the crews table. A
source that adds series to the registry while it collects sets
``REGISTERS_SERIES = True``: those run before the others, so every source
can recognise the series they add; the merge still takes the sources in
content priority order.

A source that fails is logged and left out, unless ``--strict`` is given.
The catalogue is written to a temporary file, optimised and renamed into
place, then compressed to ``<output>.gz`` with a ``.sha256`` checksum beside
it for publishing.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import importlib
import os
import pkgutil
import shutil
import sqlite3
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from . import sources as sources_package
from .context import BuildContext
from .merge import DEFAULT_PRIORITY, SourceBatch, merge_records, write_catalogue
from .records import CrewRecord, SourceInfo
from .series import SeriesRegistry

BUILDER_VERSION = "1"
REPOSITORY = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPOSITORY / "build" / "catalogue.sqlite"
DEFAULT_CACHE = Path.home() / ".cache" / "piratefinder-build"


@dataclass(frozen=True, slots=True)
class Source:
    module_name: str
    module: ModuleType
    info: SourceInfo
    priority: int
    default_enabled: bool

    def named(self, names: set[str]) -> bool:
        return self.info.id in names or self.module_name in names


def discover(
    package: ModuleType = sources_package, log: Callable[[str], None] = print
) -> tuple[list[Source], dict[str, str]]:
    """Every source module in ``package``, and the modules that failed to import."""
    found: list[Source] = []
    broken: dict[str, str] = {}
    for entry in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        if entry.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{package.__name__}.{entry.name}")
        except Exception as error:  # a broken importer must not stop the others
            broken[entry.name] = f"{type(error).__name__}: {error}"
            log(f"{entry.name}: cannot be imported: {broken[entry.name]}")
            continue
        info = getattr(module, "INFO", None)
        if not isinstance(info, SourceInfo) or not callable(getattr(module, "collect", None)):
            continue
        found.append(
            Source(
                module_name=entry.name,
                module=module,
                info=info,
                priority=int(getattr(module, "CONTENT_PRIORITY", DEFAULT_PRIORITY)),
                default_enabled=bool(getattr(module, "DEFAULT_ENABLED", True)),
            )
        )
    found.sort(key=lambda source: (source.priority, source.info.id))
    return found, broken


def _names(values: list[str]) -> set[str]:
    return {name.strip() for value in values for name in value.split(",") if name.strip()}


def select(
    available: list[Source], *, only: set[str], skip: set[str], extra: set[str]
) -> list[Source]:
    """The sources to run: ``--only`` if given, else the default ones plus ``--with``."""
    if only:
        chosen = [source for source in available if source.named(only)]
    else:
        chosen = [s for s in available if s.default_enabled or s.named(extra)]
    return [source for source in chosen if not source.named(skip)]


def _parse_inputs(values: list[str]) -> dict[str, Path]:
    inputs: dict[str, Path] = {}
    for value in values:
        source_id, separator, path = value.partition("=")
        if not separator or not source_id or not path:
            raise SystemExit(f"--input expects id=path, not {value!r}")
        inputs[source_id.strip()] = Path(path).expanduser()
    return inputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m catalogue_builder",
        description="Build the PirateFinder catalogue from public sources.",
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="download cache")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="catalogue file")
    parser.add_argument("--offline", action="store_true", help="use cached downloads only")
    parser.add_argument("--only", action="append", default=[], help="run only these sources")
    parser.add_argument("--skip", action="append", default=[], help="leave these sources out")
    parser.add_argument(
        "--with",
        dest="extra",
        action="append",
        default=[],
        help="also run these sources that are off by default",
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="read a source from a local file instead of downloading it",
    )
    parser.add_argument("--list-sources", action="store_true", help="list sources and exit")
    parser.add_argument("--strict", action="store_true", help="stop when a source fails")
    return parser


def _log(message: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


def main(
    argv: list[str] | None = None,
    log: Callable[[str], None] = _log,
    package: ModuleType = sources_package,
) -> int:
    args = build_parser().parse_args(argv)
    available, broken = discover(package, log=log)
    if args.list_sources:
        for source in available:
            state = "on" if source.default_enabled else "off"
            print(
                f"{source.info.id:18} {source.module_name:18} priority {source.priority:3} "
                f"{state:3}  {source.info.name} <{source.info.url}>"
            )
        for name, error in broken.items():
            print(f"{'-':18} {name:18} cannot be imported: {error}")
        return 0
    if broken and args.strict:
        log("stopping: a source module cannot be imported (--strict)")
        return 1

    chosen = select(
        available, only=_names(args.only), skip=_names(args.skip), extra=_names(args.extra)
    )
    if not chosen:
        log("no sources selected")
        return 1
    registry = SeriesRegistry.load()
    context = BuildContext(
        cache_dir=args.cache.expanduser(),
        series=registry,
        offline=args.offline,
        inputs=_parse_inputs(args.input),
        log=log,
    )
    started = time.monotonic()
    batches: list[SourceBatch] = []
    statuses: dict[str, str] = {f"source:{name}": f"failed: {e}" for name, e in broken.items()}
    # Sources that add series run first ("800 Degrees (Scoopex)" from TOSEC),
    # so Demozoo and amigascne key their packs by those series too instead of
    # making a second disk beside each one.
    running = sorted(chosen, key=lambda source: not registers_series(source))
    for source in running:
        log(f"{source.info.id}: collecting")
        begun = time.monotonic()
        try:
            records = list(source.module.collect(context))
        except Exception as error:
            log(f"{source.info.id}: failed: {type(error).__name__}: {error}")
            if args.strict:
                traceback.print_exc()
                return 1
            statuses[f"source:{source.info.id}"] = f"failed: {type(error).__name__}: {error}"
            continue
        seconds = time.monotonic() - begun
        log(f"{source.info.id}: {len(records)} records in {seconds:.1f} s")
        crews = collect_crews(source, context, log)
        if crews is None and args.strict:
            return 1
        batches.append(
            SourceBatch(
                info=source.info,
                records=records,
                priority=source.priority,
                status=f"ok, {len(records)} records",
                retrieved=str(getattr(source.module, "RETRIEVED", "") or ""),
                crews=crews or [],
            )
        )
    if not batches:
        log("every source failed; no catalogue written")
        return 1
    order = {source.info.id: index for index, source in enumerate(chosen)}
    batches.sort(key=lambda batch: order[batch.info.id])

    result = merge_records(batches, registry, log)
    built_at = datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()
    meta = {"built_at": built_at, "builder_version": BUILDER_VERSION, **statuses}
    stats = write_database(args.output, result, meta, context, log)
    publish(args.output, log)
    _report(stats, log)
    log(f"catalogue written to {args.output} in {time.monotonic() - started:.1f} s")
    return 0


def registers_series(source: Source) -> bool:
    return bool(getattr(source.module, "REGISTERS_SERIES", False))


def collect_crews(
    source: Source, context: BuildContext, log: Callable[[str], None]
) -> list[CrewRecord] | None:
    """The crew records of a source that has ``collect_crews``; None when that fails.

    A failure costs only the crew histories of that source: its disks are
    still merged.
    """
    collector = getattr(source.module, "collect_crews", None)
    if not callable(collector):
        return []
    try:
        crews = list(collector(context))
    except Exception as error:
        log(f"{source.info.id}: crews failed: {type(error).__name__}: {error}")
        return None
    log(f"{source.info.id}: {len(crews)} crew records")
    return crews


def write_database(
    output: Path, result, meta: dict[str, str], context: BuildContext, log: Callable[[str], None]
) -> dict[str, int]:
    """Write the catalogue to a temporary file, optimise it and move it into place."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        stats = write_catalogue(connection, result, meta=meta, groups=context.groups, log=log)
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    os.replace(temporary, output)
    return stats


def publish(output: Path, log: Callable[[str], None]) -> None:
    """Write ``<output>.gz`` and ``<output>.gz.sha256`` for a release."""
    compressed = output.with_name(output.name + ".gz")
    temporary = compressed.with_name(compressed.name + ".tmp")
    with output.open("rb") as source, gzip.open(temporary, "wb", compresslevel=9) as target:
        shutil.copyfileobj(source, target, 1 << 20)
    os.replace(temporary, compressed)
    digest = hashlib.sha256(compressed.read_bytes()).hexdigest()
    compressed.with_name(compressed.name + ".sha256").write_text(f"{digest}  {compressed.name}\n")
    log(f"{compressed.name}: {compressed.stat().st_size / 1e6:.1f} MB, sha256 {digest[:16]}")


def _report(stats: dict[str, int], log: Callable[[str], None]) -> None:
    for key in sorted(stats):
        if key.startswith("disks:"):
            _label, platform, kind = key.split(":")
            log(f"  {platform:9} {kind:12} {stats[key]:7} disks")
    for key in (
        "disks",
        "series",
        "with contents",
        "with images",
        "with locations",
        "images",
        "contents",
        "locations",
        "hash merges",
        "unmatched locations",
        "entries",
        "with year",
        "with month",
        "with day",
        "images with virus",
        "images with virus damage",
        "images with antivirus",
        "media",
        "media unmatched",
        "trivia",
        "trivia unmatched",
        "crews",
        "crews unmatched",
        "crews ambiguous",
        "crews pinned",
        "crews dominant",
        "with crew history",
    ):
        log(f"  {key:22} {stats.get(key, 0):7}")


if __name__ == "__main__":
    sys.exit(main())
