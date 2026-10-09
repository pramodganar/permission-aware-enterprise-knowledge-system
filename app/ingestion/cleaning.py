"""Tidying extracted text before it is stored.

The rule for everything here: remove what a reader can't see and make
whitespace consistent, but never change a word. Nothing is re-flowed,
de-hyphenated, lower-cased or spell-corrected, because each of those can
silently alter what a policy says.

What is removed matters for safety as well as tidiness. Invisible characters
can carry text that a person reviewing the document would never notice but a
language model would read.
"""

import re
import unicodedata

# PDF text often contains single-character ligatures. Spelled out, "ﬁnance"
# becomes "finance" and can be found by a search for it.
_LIGATURES = str.maketrans(
    {
        "ﬀ": "ff",
        "ﬁ": "fi",
        "ﬂ": "fl",
        "ﬃ": "ffi",
        "ﬄ": "ffl",
        "ﬅ": "st",
        "ﬆ": "st",
    }
)

# Zero-width joiners are invisible but change how Hindi, Persian and emoji
# are written, so they stay. Every other invisible format character goes.
_KEPT_FORMAT_CHARACTERS = frozenset("\u200c\u200d")

_SPACE_RUNS = re.compile(r" {2,}")
_BLANK_LINE_RUNS = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """Return the text with invisible characters removed and whitespace normalised.

    Cleaning text that is already clean returns it unchanged.
    """
    text = unicodedata.normalize("NFC", text.translate(_LIGATURES))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    text = "".join(_visible(character) for character in text)

    lines = (_SPACE_RUNS.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANK_LINE_RUNS.sub("\n\n", "\n".join(lines)).strip()


def _visible(character: str) -> str:
    if character == "\n":
        return character
    if character == "\t":
        return " "

    category = unicodedata.category(character)
    if category == "Zs":  # no-break space and the other Unicode spaces
        return " "
    if category == "Zl":  # line separator
        return "\n"
    if category == "Zp":  # paragraph separator
        return "\n\n"
    if category == "Cf":  # zero-width, bidirectional override and tag characters
        return character if character in _KEPT_FORMAT_CHARACTERS else ""
    if category in ("Cc", "Co"):  # control characters, private-use glyphs
        return ""
    return character
