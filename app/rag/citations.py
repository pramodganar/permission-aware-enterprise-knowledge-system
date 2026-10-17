"""Turning the labels in a model's answer into the sources they stand for.

The model is told to cite its sources as [S1], [S2]. A label is only a name
for a source in the context this answer was built from, so a citation can
only ever point to a passage the person may read: it is looked up there and
nowhere else. A label that isn't in the context, [S9] when there were five
sources, is something the model made up. It is removed from the text and
counted, never resolved to anything.

Models don't all write labels the same way. Measured on the real replies,
gpt-oss wrote [S1], [ S1 ] and the full-width 【S1】, and lists like [S1][S3]
or [S1, S3] are common elsewhere. All of them are read, and the text is
rewritten with one form, [S1], so the interface only has to handle one.

An answer that cites nothing is not thrown away: it came from the context
like any other, but there is no saying which source each statement came
from. It is marked uncited, and the interface can say so and list the
sources the model was given.
"""

import re
from dataclasses import dataclass

from app.rag.context import Context, Source

# One pair of brackets, ASCII or full-width, holding one or more labels
# separated by commas, semicolons or spaces: [S1], [ S1 ], 【S1】, [S1, S3].
# The spaces in front are part of it, so removing a citation doesn't leave a
# gap before the full stop that follows.
_CITATION = re.compile(
    r"([ \t]*)[\[【]\s*(S\d+(?:\s*[,;]?\s*S\d+)*)\s*[\]】]",
    re.IGNORECASE,
)
_LABEL = re.compile(r"S(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class Citations:
    # The answer with every citation written as [S1] and made-up ones removed.
    text: str
    # The sources cited, in the order they are first cited.
    sources: tuple[Source, ...]
    # Labels cited that weren't in the context.
    unknown: int

    @property
    def uncited(self) -> bool:
        return not self.sources


def resolve_citations(text: str, context: Context) -> Citations:
    """Find the citations in an answer and look each one up in its context."""
    cited: dict[str, Source] = {}
    unknown = 0

    def rewrite(match: re.Match[str]) -> str:
        nonlocal unknown
        kept = []
        for number in _LABEL.findall(match.group(2)):
            # S01 is S1. The number is compared, not the spelling.
            label = f"S{int(number)}"
            source = context.source(label)
            if source is None:
                unknown += 1
                continue
            cited.setdefault(label, source)
            if label not in kept:
                kept.append(label)
        if not kept:
            return ""
        return match.group(1) + "".join(f"[{label}]" for label in kept)

    rewritten = _CITATION.sub(rewrite, text).strip()
    return Citations(text=rewritten, sources=tuple(cited.values()), unknown=unknown)
