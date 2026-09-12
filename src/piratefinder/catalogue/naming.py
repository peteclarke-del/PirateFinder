"""TOSEC name parsing and text normalisation.

TOSEC names a dump as

    Title version (demo) (date)(publisher)(system)(video)(country)(language)
    (copyright)(development)(media)(label)[cr][f][h][m][p][t][tr][o][u][v][b][a][!][more]

following the TOSEC Naming Convention (https://www.tosecdev.org/tosec-naming-convention).
``parse_tosec_name`` splits such a name into its fields. It is used by the
catalogue builder to read the DATs and by the library scanner to make sense of
local file names, which often follow the same convention. Names that do not
follow it still parse: whatever is not recognised stays in the title.

``normalise`` is the one spelling used for comparing and indexing text, so a
query, an alias and a catalogue title all meet in the same form.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

# Extensions stripped from a file name even when the name does not end in a
# TOSEC field, so "Automation 250.msa" parses as "Automation 250".
KNOWN_EXTENSIONS = frozenset(
    {
        "st",
        "stx",
        "msa",
        "dim",
        "ipf",
        "adf",
        "adz",
        "dms",
        "hfe",
        "scp",
        "raw",
        "img",
        "zip",
        "7z",
        "gz",
        "lha",
        "lzx",
    }
)

_EXTENSION = re.compile(r"\.([A-Za-z0-9]{1,5})$")
_DATE = re.compile(r"^[12][0-9x]{3}(?:-[0-9x]{2}(?:-[0-9x]{2})?)?$", re.IGNORECASE)
_VERSION = re.compile(
    r"^(?P<title>.*?\S)\s+(?P<version>v\d+(?:[.\-]\d+)*[a-z]?|Rev \d+(?:\.\d+)*)$",
    re.IGNORECASE,
)
_ALTERNATE = re.compile(r"^a(\d*)(?:\s|$)")
_BAD = re.compile(r"^b\d*(?:\s|$)")
_MODIFIED = re.compile(r"^(?:h|m|f|o|u|v|p)\d*(?:\s|$)")
_MEDIA = re.compile(r"^(?:Disk|Disc|Side|Part|Tape|File)\s", re.IGNORECASE)
_DISK_OF = re.compile(r"^(?:Disk|Disc)\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
_PART = re.compile(
    r"^Part\s+([A-Za-z0-9]+)\b(?:\s+(?:Disk|Side)\s+([A-Za-z0-9]+)\b)?", re.IGNORECASE
)
_SIDE = re.compile(r"^Side\s+([A-Za-z0-9]+)\b", re.IGNORECASE)
# One title of a combined entry ends with its date and publisher fields; the
# next title follows " & ". Used to split "A (1990)(X) & B (1991)(Y)".
_COMBINED_SPLIT = re.compile(r"(?<=[)\]])\s+&\s+(?=[^()\[\]]+?\s\([12][0-9x]{3}[-0-9x]*\))")
_ARTICLE = re.compile(
    r"^(?P<head>.+?), (?P<article>The|A|An|Le|La|Les|Der|Die|Das|Il|El)\b(?P<tail>.*)$"
)
_APOSTROPHES = re.compile("['`\u2018\u2019\u00b4]")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True, slots=True)
class TosecName:
    """The fields of a TOSEC name. Every field is "" or empty when absent."""

    title: str
    version: str = ""
    date: str = ""
    publisher: str = ""
    extra: tuple[str, ...] = ()  # other () fields: "Disk 1 of 2", "AGA", "de"
    flags: tuple[str, ...] = ()  # every [] field without the brackets
    cracker: str = ""  # from [cr X]
    trainer: str = ""  # the trainer flag as written, "t", "t +2", "t +2 Band"
    modified_by: str = ""  # from [h X] and [m X]
    bad: bool = False  # [b], [b2], [b corrupt file]
    alternate: int = 0  # 0, or 1 for [a], 2 for [a2] and so on
    verified: bool = False  # [!]
    extension: str = ""  # lower case, without the dot

    @property
    def cracked(self) -> bool:
        return any(flag == "cr" or flag.startswith("cr ") for flag in self.flags)

    @property
    def trained(self) -> bool:
        return bool(self.trainer)

    @property
    def modified(self) -> bool:
        return any(_MODIFIED.match(flag) for flag in self.flags)

    @property
    def disk_of(self) -> tuple[int, int] | None:
        """(1, 2) for a "(Disk 1 of 2)" field, None when there is none."""
        for field in self.extra:
            found = _DISK_OF.match(field)
            if found:
                return int(found.group(1)), int(found.group(2))
        return None

    @property
    def part(self) -> str:
        """The disk part as the name gives it: "A" from "(Part A)" or
        "(Side A)", "LA" from "(Part L Disk A)", "1" from "(Disk 1 of 2)",
        "" when there is none."""
        for pattern in (_PART, _SIDE):
            for field in self.extra:
                found = pattern.match(field)
                if found:
                    return "".join(group for group in found.groups() if group)
        disk_of = self.disk_of
        return str(disk_of[0]) if disk_of else ""

    @property
    def media(self) -> str:
        """The first media field, for example "Disk 1 of 2", or ""."""
        return next((field for field in self.extra if _MEDIA.match(field)), "")

    @property
    def trainer_group(self) -> str:
        """Who made the trainer: "Band" from "[t +2 Band]", "" when unknown."""
        words = self.trainer.split()[1:]
        if words and words[0].startswith("+"):
            words = words[1:]
        return " ".join(words)

    @property
    def trainer_count(self) -> str:
        """ "+2" from "[t +2 Band]", "" when the flag gives no count."""
        words = self.trainer.split()[1:]
        return words[0] if words and words[0].startswith("+") else ""


def split_combined(name: str) -> list[str]:
    """Split a combined entry into its titles.

    TOSEC names a disk holding several releases as
    "A (1990)(X)[cr Y] & B (1991)(Z)". Each part keeps its own fields; a
    trailing "-[a]" applies to the whole entry and stays on the last part.
    A plain name comes back as a one-element list.
    """
    return [part.strip() for part in _COMBINED_SPLIT.split(name.strip()) if part.strip()]


def parse_tosec_name(name: str) -> TosecName:
    """Split a TOSEC name, with or without a file extension, into its fields."""
    text, extension = _strip_extension(name.strip())
    parts = split_combined(text)
    if len(parts) > 1:
        return _combine([_parse_single(part) for part in parts], extension)
    return _parse_single(text, extension)


def _strip_extension(text: str) -> tuple[str, str]:
    found = _EXTENSION.search(text)
    if found is None:
        return text, ""
    before = text[: found.start()]
    extension = found.group(1).lower()
    if extension in KNOWN_EXTENSIONS or before.endswith((")", "]")):
        return before, extension
    return text, ""


def _take_trailing(text: str, opening: str, closing: str) -> tuple[str, str] | None:
    """Remove one trailing bracketed field; None when there is none to take."""
    stripped = text.rstrip()
    if not stripped.endswith(closing):
        return None
    depth = 0
    for index in range(len(stripped) - 1, -1, -1):
        character = stripped[index]
        if character == closing:
            depth += 1
        elif character == opening:
            depth -= 1
            if depth == 0:
                rest = stripped[:index]
                if not rest.strip():
                    return None
                return rest, stripped[index + 1 : -1].strip()
    return None


def _parse_single(text: str, extension: str = "") -> TosecName:
    flags: list[str] = []
    while (taken := _take_trailing(text, "[", "]")) is not None:
        text, flag = taken
        flags.insert(0, flag)
        text = text.rstrip()
        if text.endswith(")-"):
            text = text[:-1]
    fields: list[str] = []
    while (taken := _take_trailing(text, "(", ")")) is not None:
        text, field = taken
        fields.insert(0, field)
    title = text.strip()

    date = publisher = ""
    extra: list[str] = []
    date_index = next((i for i, field in enumerate(fields) if _DATE.match(field)), None)
    if date_index is None:
        extra = fields
    else:
        date = fields[date_index]
        extra = fields[:date_index]
        after = fields[date_index + 1 :]
        if after:
            publisher = "" if after[0] == "-" else after[0]
            extra += after[1:]

    version = ""
    found = _VERSION.match(title)
    if found:
        title, version = found.group("title"), found.group("version")

    crackers: list[str] = []
    modifiers: list[str] = []
    trainer = ""
    bad = verified = False
    alternate = 0
    for flag in flags:
        head, _space, rest = flag.partition(" ")
        if head == "cr":
            if rest.strip():
                crackers.append(rest.strip())
        elif head == "t":
            trainer = flag
        elif head in ("h", "m") and rest.strip():
            modifiers.append(rest.strip())
        elif flag == "!":
            verified = True
        elif _BAD.match(flag):
            bad = True
        elif (alternate_match := _ALTERNATE.match(flag)) is not None and not alternate:
            alternate = int(alternate_match.group(1) or 1)
    return TosecName(
        title=title,
        version=version,
        date=date,
        publisher=publisher,
        extra=tuple(extra),
        flags=tuple(flags),
        cracker=" - ".join(_distinct(crackers)),
        trainer=trainer,
        modified_by=" - ".join(_distinct(modifiers)),
        bad=bad,
        alternate=alternate,
        verified=verified,
        extension=extension,
    )


def _combine(parts: list[TosecName], extension: str) -> TosecName:
    first = parts[0]
    flags = tuple(flag for part in parts for flag in part.flags)
    return TosecName(
        title=" & ".join(part.title for part in parts),
        version="",
        date=first.date,
        publisher=" - ".join(_distinct(part.publisher for part in parts if part.publisher)),
        extra=tuple(_distinct(field for part in parts for field in part.extra)),
        flags=flags,
        cracker=" - ".join(
            _distinct(name for part in parts if part.cracker for name in part.cracker.split(" - "))
        ),
        trainer=next((part.trainer for part in parts if part.trainer), ""),
        modified_by=" - ".join(_distinct(p.modified_by for p in parts if p.modified_by)),
        bad=any(part.bad for part in parts),
        alternate=max(part.alternate for part in parts),
        verified=all(part.verified for part in parts),
        extension=extension,
    )


def _distinct(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def display_title(title: str) -> str:
    """Move a trailing article back to the front: "Chaos Engine, The" ->
    "The Chaos Engine", "Chaos Engine, The - Demo" -> "The Chaos Engine - Demo"."""
    found = _ARTICLE.match(title)
    if found is None:
        return title
    tail = found.group("tail")
    definite = found.group("article") not in ("A", "An")
    if tail and not tail.startswith((" - ", ":", " (")) and not (definite and tail[0] == " "):
        return title
    return f"{found.group('article')} {found.group('head')}{tail}"


def tidy_label(name: str) -> str:
    """A short display label for a disk known only by its TOSEC name.

    "Chaos Engine, The (1993)(Renegade)(Disk 1 of 2)[cr Cynix][a]" becomes
    "The Chaos Engine (Disk 1 of 2) [cr Cynix]".
    """
    parts = [_parse_single(part) for part in split_combined(_strip_extension(name.strip())[0])]
    titles = []
    for parsed in parts:
        text = display_title(parsed.title)
        if parsed.version:
            text = f"{text} {parsed.version}"
        titles.append(text)
    label = " & ".join(titles)
    if len(parts) == 1:
        media = parts[0].media
        if media:
            label = f"{label} ({media})"
        if parts[0].cracker:
            label = f"{label} [cr {parts[0].cracker}]"
    return label or name.strip()


def normalise(text: str) -> str:
    """Case-folded words without diacritics or punctuation, for comparison.

    Apostrophes are dropped ("Xad's" -> "xads"), "&" becomes "and", and every
    other run of punctuation or space becomes one space.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    stripped = _APOSTROPHES.sub("", stripped.replace("&", " and "))
    return " ".join(_NON_WORD.sub(" ", stripped).split())


def sort_key(text: str) -> str:
    """Order names the way people expect: "Automation 9" before "Automation 10"."""
    return _NUMBER.sub(lambda found: found.group().zfill(8), normalise(text))


def image_rank_key(flags: str | Iterable[str]) -> tuple[int, int, int]:
    """Sort key for dumps of one disk, lowest first.

    Verified dumps ([!]) come first, then clean ones, then alternates in
    order ([a], [a2], ...), then modified, hacked, fixed or trained dumps,
    and bad dumps ([b]) last.
    """
    if isinstance(flags, str):
        flags = re.findall(r"\[([^\]]*)\]", flags)
    flags = tuple(flags)
    bad = any(_BAD.match(flag) for flag in flags)
    changed = any(_MODIFIED.match(flag) or flag == "t" or flag.startswith("t ") for flag in flags)
    alternate = 0
    for flag in flags:
        found = _ALTERNATE.match(flag)
        if found:
            alternate = int(found.group(1) or 1)
            break
    verified = "!" in flags
    return (2 if bad else 1 if changed else 0, alternate, 0 if verified else 1)
