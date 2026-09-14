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


def crew_key(name: str) -> str:
    """A crew name for comparison: "The Medway Boys" and "Medway Boys" agree,
    and so do "Tristar & Red Sector Inc" and "Tristar and Red Sector Inc.",
    and "The Droog's" and "Droogs"."""
    text = re.sub(r"['\u2019]", "", name).replace("&", " and ")
    return re.sub(r"^the ", "", normalise(text))


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
    way of writing a joint release, and expands each of them. A group entry
    may list ``platforms``: its abbreviations and aliases then mean it only on
    those platforms ("ICS" is one crew on the Atari ST and another on the
    Amiga), and are expanded only when the caller names the platform; a
    caller that names none gets the aliases of any platform.
    """

    def __init__(
        self,
        abbreviations: dict[str, str],
        aliases: dict[str, str],
        platform_abbreviations: dict[tuple[str, str], str] | None = None,
        platform_aliases: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._abbreviations = abbreviations
        self._aliases = aliases
        self._platform_abbreviations = platform_abbreviations or {}
        self._platform_aliases = platform_aliases or {}
        self._any_platform_aliases = {
            key: name for (key, _p), name in self._platform_aliases.items()
        }

    @classmethod
    def load(cls, path: Path = GROUPS_FILE) -> GroupRegistry:
        abbreviations: dict[str, str] = {}
        aliases: dict[str, str] = {}
        by_platform: dict[tuple[str, str], str] = {}
        aliases_by_platform: dict[tuple[str, str], str] = {}
        if path.exists():
            with path.open("rb") as handle:
                document = tomllib.load(handle)
            for entry in document.get("group", []):
                name = entry["name"]
                aliases[normalise(name)] = name
                platforms = entry.get("platforms", [])
                for tag in entry.get("abbreviations", []):
                    targets = [(tag, platform) for platform in platforms] if platforms else [tag]
                    table = by_platform if platforms else abbreviations
                    for target in targets:
                        if table.get(target, name) != name:
                            raise ValueError(f"{path.name}: {target!r} names two groups")
                        table[target] = name
                for alias in entry.get("aliases", []):
                    if platforms:
                        for platform in platforms:
                            aliases_by_platform[(normalise(alias), platform)] = name
                    else:
                        aliases[normalise(alias)] = name
        return cls(abbreviations, aliases, by_platform, aliases_by_platform)

    def expand_one(self, tag: str, platform: str = "") -> str:
        text = tag.strip()
        if platform and (text, platform) in self._platform_abbreviations:
            return self._platform_abbreviations[(text, platform)]
        if text in self._abbreviations:
            return self._abbreviations[text]
        key = normalise(text)
        if platform:
            found = self._platform_aliases.get((key, platform))
        else:
            found = self._any_platform_aliases.get(key)
        return found or self._aliases.get(key, text)

    def expand(self, text: str, platform: str = "") -> str:
        names = [self.expand_one(part, platform) for part in text.split(" - ") if part.strip()]
        return " - ".join(dict.fromkeys(names))

    def crew_keys(self, name: str, platform: str = "") -> set[str]:
        """The ``crew_key`` of a crew name as written and expanded; two names
        mean the same crew when their keys meet."""
        expanded = self.expand_one(name, platform)
        return {key for key in (crew_key(name), crew_key(expanded)) if key}

    def spellings(self, text: str, platform: str = "") -> list[str]:
        """Every spelling of the groups in ``text``: the tags as given and the
        expanded names, for the search index."""
        found: dict[str, None] = {}
        for part in text.split(" - "):
            if part.strip():
                found.setdefault(part.strip(), None)
                found.setdefault(self.expand_one(part, platform), None)
        return list(found)


CREW_PINS_FILE = DATA_DIR.parent / "crew-pins.toml"


@dataclass(frozen=True, slots=True)
class CrewChoice:
    """How the merge picks one of several crews of one name that a source has
    on one platform, when the source credits none of them with a disk
    (data/crew-pins.toml).

    A pin names the crew a name stands for on a platform, as the source's own
    id for it. Without a pin, a crew that has at least ``dominant_share`` of
    the releases the source credits to all crews of that name on that
    platform, and at least ``dominant_releases`` of them, is taken.
    """

    dominant_share: float
    dominant_releases: int
    pins: dict[tuple[str, str, str], str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = CREW_PINS_FILE) -> CrewChoice:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        pins: dict[tuple[str, str, str], str] = {}
        for entry in document.get("pin", []):
            key = (crew_key(entry["name"]), entry["platform"], entry["source"])
            if key in pins:
                raise ValueError(f"{path.name}: {entry['name']!r} is pinned twice")
            pins[key] = str(entry["id"])
        return cls(float(document["dominant_share"]), int(document["dominant_releases"]), pins)

    def pinned(self, keys: set[str], platform: str, source: str) -> str | None:
        """The pinned crew id for a crew name (as ``crew_keys``) on a platform."""
        for key in sorted(keys):
            found = self.pins.get((key, platform, source))
            if found is not None:
                return found
        return None

    def dominant(self, releases: dict[int, int]) -> int | None:
        """The one crew of ``releases`` (crew -> releases on the platform) that
        has most of them, or None when none has enough."""
        total = sum(releases.values())
        if not total:
            return None
        crew, count = max(releases.items(), key=lambda item: (item[1], -item[0]))
        if count >= self.dominant_releases and count >= self.dominant_share * total:
            return crew
        return None
