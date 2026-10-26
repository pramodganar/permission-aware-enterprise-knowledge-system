"""Turning what a person typed into something that can be searched with.

A question is untrusted input, like an uploaded file. Here it is checked,
tidied in the same way document text was tidied, and embedded.

Two things this module deliberately does not do:

- It does not decide what the person may read. Nothing in a question can
  widen or narrow access; that comes from the database, from who is asking.
- It does not try to detect or remove instructions aimed at the language
  model. That can't be done reliably, and the design doesn't depend on it:
  the model is only ever given text the person may read.

Questions can be as sensitive as documents ("is my colleague on a performance
plan?"), so the text is never logged here. Only its size is.
"""

import logging
import re
import time
from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import InvalidInputError
from app.core.logging_setup import milliseconds_since
from app.ingestion.cleaning import clean_text
from app.retrieval.embeddings import Embedder, Vectors, embed_question

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ProcessedQuestion:
    # The question as it will be searched with, shown back and written to the audit log.
    text: str
    vector: Vectors
    tokens: int


def clean_question(raw: str, settings: Settings) -> str:
    """Return the question on one line, tidied the way document text is.

    Raises InvalidInputError if it is empty, too long, or has no words in it.
    """
    # Checked first, so a huge input is refused before any work is done on it.
    if len(raw) > settings.max_question_characters:
        raise InvalidInputError(_too_long(settings))

    # The same cleaning as at ingestion: a ligature or an invisible character
    # pasted into a question would otherwise stop it matching the documents.
    text = _WHITESPACE.sub(" ", clean_text(raw)).strip()
    if not text:
        raise InvalidInputError("the question is empty")
    # Spelling out a ligature can make the text a little longer.
    if len(text) > settings.max_question_characters:
        raise InvalidInputError(_too_long(settings))
    if not any(character.isalnum() for character in text):
        raise InvalidInputError("the question has no words in it")
    return text


def process_question(raw: str, settings: Settings, embedder: Embedder) -> ProcessedQuestion:
    """Check, tidy and embed a question."""
    started = time.perf_counter()
    text = clean_question(raw, settings)

    # The model would embed the start of an over-long question and ignore the
    # rest. A search on half a question gives an answer to something else.
    tokens = embedder.count_tokens(text)
    if tokens > embedder.max_tokens:
        raise InvalidInputError("the question is too long")

    vector = embed_question(text, embedder)
    logger.info(
        "question processed",
        extra={
            "characters": len(text),
            "tokens": tokens,
            "milliseconds": milliseconds_since(started),
        },
    )
    return ProcessedQuestion(text=text, vector=vector, tokens=tokens)


def _too_long(settings: Settings) -> str:
    return f"the question is longer than {settings.max_question_characters} characters"
