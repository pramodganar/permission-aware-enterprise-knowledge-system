"""The evaluation set: questions about AcmeCorp, each asked by one of its people.

The questions live in ``questions.json`` next to this file, versioned with
the code. Each one says who asks it, what kind of question it is, and what
a right response looks like:

    answerable   the asker may read a document that answers it
    related      near a document the asker may read, but no document answers it
    off_topic    nothing in the company documents is about it
    forbidden    the answer is in a document the asker may not read
    injection    a forbidden question dressed up to talk its way past the rules

Relevant chunks are not listed by ID. A chunk ID changes whenever the
documents are re-chunked, and a set tied to IDs would silently go stale.
Instead an answerable question gives ``evidence``: short quotes from the
document that hold the answer. The relevant chunks are the ones of that
document whose text contains a quote, worked out from whatever the current
chunks are.

A forbidden question gives ``secrets`` instead: strings that are in the
document that answers it and in no other, so finding one in a response is
a leak and not a coincidence. And a ``control``: someone who may read that
document, who should get the answer, so a refusal is shown to come from the
permission and not from a missing document.

The checks that the set is what it says (every quote is in its document,
every secret in one document only, every asker allowed or not as claimed)
are tests: tests/integration/test_evaluation_set.py.
"""

import json
import re
from collections import Counter
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

QUESTIONS_FILE = Path(__file__).with_name("questions.json")

PEOPLE = ("alice", "bob", "carol", "david", "emma", "frank", "grace", "henry", "irene")

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=60)]
Person = Annotated[str, StringConstraints(pattern=r"^(" + "|".join(PEOPLE) + r")$")]
Text = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


class Kind(StrEnum):
    ANSWERABLE = "answerable"
    RELATED = "related"
    OFF_TOPIC = "off_topic"
    FORBIDDEN = "forbidden"
    INJECTION = "injection"


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Slug
    kind: Kind
    asker: Person
    question: Text
    # The document that answers it; for a related question, the one it is near.
    document: Text | None = None
    # Answerable: quotes from the document that hold the answer.
    evidence: tuple[Text, ...] = ()
    # Answerable: what a right answer says, for judging answers.
    reference: Text | None = None
    # Forbidden and injection: strings only the forbidden document contains.
    secrets: tuple[Text, ...] = ()
    # Forbidden and injection: someone who may read the document.
    control: Person | None = None
    # Why the question is in the set, when it isn't obvious.
    note: Text | None = None

    @model_validator(mode="after")
    def fields_fit_the_kind(self) -> "Question":
        wanted = {
            Kind.ANSWERABLE: {"document", "evidence", "reference"},
            Kind.RELATED: {"document", "note"},
            Kind.OFF_TOPIC: set(),
            Kind.FORBIDDEN: {"document", "secrets", "control"},
            Kind.INJECTION: {"document", "secrets", "control"},
        }[self.kind]
        given = {
            name
            for name in ("document", "evidence", "reference", "secrets", "control")
            if getattr(self, name)
        }
        missing = wanted - given - {"note"}
        extra = given - wanted
        if missing or extra:
            raise ValueError(
                f"{self.id}: a {self.kind.value} question needs {sorted(missing) or 'nothing more'}"
                f" and must not have {sorted(extra) or 'anything else'}"
            )
        if self.kind is Kind.RELATED and not self.note:
            raise ValueError(f"{self.id}: say why no document answers it")
        if self.control is not None and self.control == self.asker:
            raise ValueError(f"{self.id}: the control must be someone else")
        return self


class QuestionSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    questions: tuple[Question, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ids_and_questions_are_unique(self) -> "QuestionSet":
        ids = Counter(q.id for q in self.questions)
        asked = Counter((q.asker, normalise(q.question)) for q in self.questions)
        repeated = [i for i, n in ids.items() if n > 1]
        if repeated:
            raise ValueError(f"repeated ids: {repeated}")
        twice = [f"{who}: {text}" for (who, text), n in asked.items() if n > 1]
        if twice:
            raise ValueError(f"asked twice by the same person: {twice}")
        return self

    def of_kind(self, *kinds: Kind) -> list[Question]:
        return [q for q in self.questions if q.kind in kinds]

    def counts(self) -> dict[str, int]:
        found = Counter(q.kind.value for q in self.questions)
        return {kind.value: found.get(kind.value, 0) for kind in Kind}


_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Whitespace folded, so a quote matches across the line breaks in a PDF's text."""
    return _SPACE.sub(" ", text).strip().casefold()


def contains(text: str, quote: str) -> bool:
    return normalise(quote) in normalise(text)


def load(path: Path = QUESTIONS_FILE) -> QuestionSet:
    return QuestionSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


@cache
def default_set() -> QuestionSet:
    return load()
