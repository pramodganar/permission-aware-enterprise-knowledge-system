"""Turning an uploaded file into text.

One function per file type, looked up by extension. An uploaded file is
untrusted input: an extractor must either return text or raise
InvalidInputError, and never let a parser error escape.
"""

from collections.abc import Callable, Mapping
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType

from app.core.exceptions import InvalidInputError

Extractor = Callable[[bytes], str]


def extract_txt(data: bytes) -> str:
    try:
        # utf-8-sig also accepts plain UTF-8 and drops a leading byte order mark.
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidInputError("the file is not valid UTF-8 text") from None


EXTRACTORS: Mapping[str, Extractor] = MappingProxyType({".txt": extract_txt})


def base_name(filename: str) -> str:
    """Return the file name without any directory part, whichever slashes were used."""
    return PurePosixPath(PureWindowsPath(filename).name).name


def extension_of(filename: str) -> str:
    return PurePosixPath(base_name(filename)).suffix.lower()


def extract_text(filename: str, data: bytes) -> str:
    """Extract text using the extractor for this file's extension."""
    extractor = EXTRACTORS.get(extension_of(filename))
    if extractor is None:
        supported = ", ".join(sorted(EXTRACTORS))
        raise InvalidInputError(f"unsupported file type; supported types: {supported}")
    return extractor(data)
