"""Series definitions and the rules that recognise them in each source.

Nothing about any particular crew or series is written into code. Every series
is described in ``data/series/*.toml``:

    [[series]]
    id = "automation"
    name = "Automation"
    platform = "atari-st"
    kind = "menu"
    group = "Automation"
    label = "Automation {number}{part_suffix}{version_suffix}"
    aliases = ["automation", "auto", "a"]

    [[match]]
    series = "automation"
    source = "tosec"
    patterns = ['^Automation Menu Disk (?P<number>\\d+)']

Patterns are Python regular expressions matched case-insensitively against the
name a source uses for a disk (or for a whole set, for sources such as Atari
Legend that name sets rather than disks). Named groups ``number``, ``part``
and ``version`` are picked up when present. Several files may add ``[[match]]``
tables for the same series, so each importer keeps its rules in its own file.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "series"
DEFAULT_LABEL = "{name} {number}{part_suffix}{version_suffix}"


def slug(text: str) -> str:
    """A stable identifier from a free-text name: "D-Bug Menu" -> "d-bug-menu"."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def normalise(text: str) -> str:
    """Case-folded words only, used to compare names from different sources."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded.lower()).split())


@dataclass(slots=True)
class SeriesDef:
    id: str
    name: str
    platform: str
    kind: str
    group: str = ""
    label: str = DEFAULT_LABEL
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    patterns: dict[str, list[re.Pattern[str]]] = field(default_factory=dict)

    def format_label(self, number: int | None, part: str = "", version: str = "") -> str:
        if number is None:
            return self.name
        return self.label.format(
            name=self.name,
            number=number,
            number3=f"{number:03d}",
            part=part,
            part_suffix=f" {part}" if part else "",
            version=version,
            version_suffix=f" {version}" if version else "",
        ).strip()


@dataclass(frozen=True, slots=True)
class SeriesMatch:
    series_id: str
    number: int | None
    part: str
    version: str


class SeriesRegistry:
    def __init__(self, definitions: dict[str, SeriesDef]) -> None:
        self._series = definitions

    @classmethod
    def load(cls, directory: Path = DATA_DIR) -> SeriesRegistry:
        definitions: dict[str, SeriesDef] = {}
        matches: list[dict] = []
        for path in sorted(directory.glob("*.toml")):
            with path.open("rb") as handle:
                document = tomllib.load(handle)
            for entry in document.get("series", []):
                if entry["id"] in definitions:
                    raise ValueError(f"{path.name}: series {entry['id']!r} is defined twice")
                definitions[entry["id"]] = SeriesDef(
                    id=entry["id"],
                    name=entry["name"],
                    platform=entry["platform"],
                    kind=entry["kind"],
                    group=entry.get("group", ""),
                    label=entry.get("label", DEFAULT_LABEL),
                    aliases=list(entry.get("aliases", [])),
                    description=entry.get("description", ""),
                )
            matches.extend({**entry, "_file": path.name} for entry in document.get("match", []))
        for entry in matches:
            series = definitions.get(entry["series"])
            if series is None:
                raise ValueError(f"{entry['_file']}: unknown series {entry['series']!r}")
            compiled = [re.compile(pattern, re.IGNORECASE) for pattern in entry["patterns"]]
            series.patterns.setdefault(entry["source"], []).extend(compiled)
        return cls(definitions)

    def get(self, series_id: str) -> SeriesDef | None:
        return self._series.get(series_id)

    def all(self) -> list[SeriesDef]:
        return list(self._series.values())

    def add(self, definition: SeriesDef) -> SeriesDef:
        """Register a series discovered in a source rather than declared."""
        return self._series.setdefault(definition.id, definition)

    def match(self, source: str, text: str, platform: str | None = None) -> SeriesMatch | None:
        """Recognise ``text`` using the patterns declared for ``source``."""
        for series in self._series.values():
            if platform is not None and series.platform != platform:
                continue
            for pattern in series.patterns.get(source, ()):
                found = pattern.search(text)
                if found is None:
                    continue
                groups = found.groupdict()
                number = groups.get("number")
                return SeriesMatch(
                    series.id,
                    int(number) if number else None,
                    (groups.get("part") or "").strip().upper(),
                    (groups.get("version") or "").strip().lower(),
                )
        return None

    def by_name(self, name: str, platform: str | None = None) -> SeriesDef | None:
        """Find a series whose name or alias equals ``name`` once normalised."""
        wanted = normalise(name)
        for series in self._series.values():
            if platform is not None and series.platform != platform:
                continue
            if wanted == normalise(series.name) or wanted in (normalise(a) for a in series.aliases):
                return series
        return None


GROUPS_FILE = DATA_DIR.parent / "groups.toml"


class GroupRegistry:
    """Crew names and the abbreviations sources use for them (data/groups.toml).

    ``expand("QTX")`` gives "Quartex"; a tag that is not listed comes back as
    it was. ``expand`` also accepts several groups joined by " - ", the TOSEC
    way of writing a joint release, and expands each of them.
    """

    def __init__(self, abbreviations: dict[str, str], aliases: dict[str, str]) -> None:
        self._abbreviations = abbreviations
        self._aliases = aliases

    @classmethod
    def load(cls, path: Path = GROUPS_FILE) -> GroupRegistry:
        abbreviations: dict[str, str] = {}
        aliases: dict[str, str] = {}
        if path.exists():
            with path.open("rb") as handle:
                document = tomllib.load(handle)
            for entry in document.get("group", []):
                name = entry["name"]
                aliases[normalise(name)] = name
                for tag in entry.get("abbreviations", []):
                    abbreviations[tag] = name
                for alias in entry.get("aliases", []):
                    aliases[normalise(alias)] = name
        return cls(abbreviations, aliases)

    def expand_one(self, tag: str) -> str:
        text = tag.strip()
        if text in self._abbreviations:
            return self._abbreviations[text]
        return self._aliases.get(normalise(text), text)

    def expand(self, text: str) -> str:
        names = [self.expand_one(part) for part in text.split(" - ") if part.strip()]
        return " - ".join(dict.fromkeys(names))

    def spellings(self, text: str) -> list[str]:
        """Every spelling of the groups in ``text``: the tags as given and the
        expanded names, for the search index."""
        found: dict[str, None] = {}
        for part in text.split(" - "):
            if part.strip():
                found.setdefault(part.strip(), None)
                found.setdefault(self.expand_one(part), None)
        return list(found)
