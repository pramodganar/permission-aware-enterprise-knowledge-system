"""Answers to the evaluation set, and what can be scored in them without judging.

    python -m evaluation.answers [--model groq|worst] [--output PATH]

Every answerable, related and off-topic question is asked by its asker
through ``answer_question``, the same single path to the model the service
uses, over a freshly loaded company. Forbidden and injection questions are
measured through the API by ``evaluation.security``.

What each answer was given is written down with it: every source, its
document and its text, exactly as the model read it. So the answers can be
scored, judged and labelled by hand later from the file alone, and a judge
or a person reading them sees what the model saw and nothing else. Every
source is a passage the asker may read, so nothing in the file is more than
its asker could have been shown.

Scored here, by rule:

    answerable   answered; given the evidence (a source contains an
                 evidence quote, the most the model could do); cites the
                 evidence (a cited source contains a quote); cites its
                 document; cut off; uncited; made-up citations
    related and  abstained, with exactly the no-information response, and
    off-topic    whether the relevance gate or the model abstained

Whether an answer is right, and whether what it says is in its sources, is
for a judge: ``evaluation.judge``.

A question whose model call failed, even after retrying, is recorded as
failed and scored as nothing: not a refusal, not an abstention.
"""

import argparse
import hashlib
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.rag.answer import NO_INFORMATION_MESSAGE, Answer, answer_question
from app.rag.llm import LLMError, LLMProvider, LLMUnavailableError
from app.rag.prompt import PROMPT_VERSION
from evaluation.company import Company, loaded_company
from evaluation.dataset import Kind, Question, QuestionSet, contains, default_set
from evaluation.models import ModelChoice, provider_for
from evaluation.retrieval import RESULTS, write
from evaluation.security import GROQ_PAUSE, GROQ_RETRIES, GROQ_RETRY_WAIT, settings_for

ASKED = (Kind.ANSWERABLE, Kind.RELATED, Kind.OFF_TOPIC)

Status = Literal["answered", "no_information", "failed"]


class GivenSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    document: str
    chunk_ids: tuple[int, ...]
    text: str


class Generated(BaseModel):
    """One question's answer, with everything the model was given for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: Kind
    asker: str
    question: str
    status: Status
    # Why there is no answer: nothing_relevant, not_in_sources,
    # refused_by_provider; or for a failed one, the error.
    reason: str | None = None
    text: str = ""
    cited: tuple[str, ...] = ()
    unknown_citations: int = 0
    cut_off: bool = False
    sources: tuple[GivenSource, ...] = ()

    @property
    def digest(self) -> str:
        """Names this exact answer, so a label written for it can't be applied to another."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    def source(self, label: str) -> GivenSource:
        return next(s for s in self.sources if s.label == label)


class Answers(BaseModel):
    """A run's answers, as written to the results file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    prompt_version: str
    settings: dict[str, Any]
    answers: tuple[Generated, ...]
    summary: dict[str, Any]

    def get(self, question_id: str) -> Generated:
        return next(a for a in self.answers if a.id == question_id)


def generated(question: Question, answer: Answer) -> Generated:
    return Generated(
        id=question.id,
        kind=question.kind,
        asker=question.asker,
        question=question.question,
        status="answered" if answer.answered else "no_information",
        reason=None if answer.reason is None else answer.reason.value,
        text=answer.text,
        cited=tuple(source.label for source in answer.citations),
        unknown_citations=answer.unknown_citations,
        cut_off=answer.cut_off,
        sources=tuple(
            GivenSource(
                label=source.label,
                document=source.document_title,
                chunk_ids=source.chunk_ids,
                text=source.text,
            )
            for source in answer.context.sources
        ),
    )


def ask(
    company: Company,
    provider: LLMProvider,
    question: Question,
    *,
    pause: float = 0.0,
    retry_wait: float = 0.0,
    retries: int = 0,
    sleep: Callable[[float], None] = time.sleep,
) -> Generated:
    """Ask one question as its asker, waiting out a provider that is rate-limiting."""
    asker = company.person(question.asker)
    for attempt in range(retries + 1):
        if pause:
            sleep(pause)
        try:
            answer = answer_question(
                company.db,
                company.index,
                company.embedder,
                provider,
                asker,
                question.question,
                company.settings,
                sleep=sleep,
            )
        except LLMUnavailableError as error:
            if attempt == retries:
                return failed(question, error)
            sleep(retry_wait)
        except LLMError as error:
            return failed(question, error)
        else:
            return generated(question, answer)
    raise AssertionError("unreachable")  # pragma: no cover


def failed(question: Question, error: Exception) -> Generated:
    return Generated(
        id=question.id,
        kind=question.kind,
        asker=question.asker,
        question=question.question,
        status="failed",
        reason=type(error).__name__,
    )


def count(flags: Iterable[bool]) -> int:
    return sum(1 for flag in flags if flag)


def has_evidence(source: GivenSource, question: Question) -> bool:
    return source.document == question.document and any(
        contains(source.text, quote) for quote in question.evidence
    )


def score(answers: Iterable[Generated], questions: QuestionSet) -> dict[str, Any]:
    """Everything that can be scored by rule, from the answers alone."""
    by_id = {q.id: q for q in questions.questions}
    listed = list(answers)
    measured = [a for a in listed if a.status != "failed"]
    answerable = [a for a in measured if a.kind is Kind.ANSWERABLE]
    answered = [a for a in answerable if a.status == "answered"]
    others = [a for a in measured if a.kind is not Kind.ANSWERABLE]

    def given_evidence(a: Generated) -> bool:
        return any(has_evidence(s, by_id[a.id]) for s in a.sources)

    def cites_evidence(a: Generated) -> bool:
        return any(has_evidence(a.source(label), by_id[a.id]) for label in a.cited)

    def cites_document(a: Generated) -> bool:
        return any(a.source(label).document == by_id[a.id].document for label in a.cited)

    citations = [(a, label) for a in answered for label in a.cited]
    abstained = [a for a in others if a.text == NO_INFORMATION_MESSAGE and not a.cited]
    return {
        "asked": len(listed),
        "failed": [a.id for a in listed if a.status == "failed"],
        "answerable": {
            "measured": len(answerable),
            "answered": len(answered),
            "given_the_evidence": count(given_evidence(a) for a in answerable),
            "answered_citing_the_evidence": count(cites_evidence(a) for a in answered),
            "answered_citing_its_document": count(cites_document(a) for a in answered),
            # Of all the citations in the answers, those to the question's
            # document. Another document can be right to cite, so this is a
            # rate to watch, not an error count.
            "citations": len(citations),
            "citations_to_its_document": count(
                a.source(label).document == by_id[a.id].document for a, label in citations
            ),
            "cut_off": count(a.cut_off for a in answered),
            "uncited": count(not a.cited for a in answered),
            "with_made_up_citations": count(a.unknown_citations > 0 for a in answered),
            "not_answered": [a.id for a in answerable if a.status != "answered"],
            "answered_without_citing_the_evidence": [
                a.id for a in answered if not cites_evidence(a)
            ],
        },
        "abstention": {
            kind.value: {
                "measured": count(a.kind is kind for a in others),
                "abstained": count(a.kind is kind for a in abstained),
                "by_the_gate": count(
                    a.kind is kind and a.reason == "nothing_relevant" for a in abstained
                ),
                "by_the_model": count(
                    a.kind is kind and a.reason != "nothing_relevant" for a in abstained
                ),
                "answered": [a.id for a in others if a.kind is kind and a not in abstained],
            }
            for kind in (Kind.RELATED, Kind.OFF_TOPIC)
        },
    }


def run(
    company: Company,
    provider: LLMProvider,
    questions: QuestionSet,
    *,
    pause: float = 0.0,
    retry_wait: float = 0.0,
    retries: int = 0,
    sleep: Callable[[float], None] = time.sleep,
) -> Answers:
    answers = tuple(
        ask(
            company,
            provider,
            q,
            pause=pause,
            retry_wait=retry_wait,
            retries=retries,
            sleep=sleep,
        )
        for q in questions.of_kind(*ASKED)
    )
    return Answers(
        model=provider.model,
        prompt_version=PROMPT_VERSION,
        settings=described(company.settings),
        answers=answers,
        summary=score(answers, questions),
    )


def described(settings: Settings) -> dict[str, Any]:
    return {
        "embedding_model": settings.embedding_model,
        "relevance_threshold": settings.retrieval_min_score,
        "top_k": settings.retrieval_top_k,
        "reranker_model": settings.reranker_model,
        "max_output_tokens": settings.llm_max_output_tokens,
    }


def lines(summary: dict[str, Any]) -> list[str]:
    a = summary["answerable"]
    out = [
        f"Answerable: answered {a['answered']} of {a['measured']}, "
        f"given the evidence {a['given_the_evidence']}, "
        f"citing the evidence {a['answered_citing_the_evidence']}, "
        f"citing its document {a['answered_citing_its_document']}",
        f"Citations to the question's document: {a['citations_to_its_document']} "
        f"of {a['citations']}",
        f"Cut off {a['cut_off']}, uncited {a['uncited']}, "
        f"with made-up citations {a['with_made_up_citations']}",
    ]
    for kind, b in summary["abstention"].items():
        out.append(
            f"{kind}: abstained {b['abstained']} of {b['measured']} "
            f"(gate {b['by_the_gate']}, model {b['by_the_model']})"
            + (f", answered: {', '.join(b['answered'])}" if b["answered"] else "")
        )
    out.append(f"Failed: {', '.join(summary['failed']) or 'none'}")
    return out


def load(path: Path) -> Answers:
    return Answers.model_validate_json(path.read_text(encoding="utf-8"))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Answer the evaluation set and score it by rule.")
    parser.add_argument("--model", choices=["worst", "groq"], default="groq")
    parser.add_argument("--output", type=Path, help="where to write the answers (JSON)")
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    choice: ModelChoice = arguments.model
    groq = choice == "groq"
    output = arguments.output or RESULTS / f"answers-{choice}.json"
    with loaded_company(settings_for(choice)) as company:
        answers = run(
            company,
            provider_for(choice, company.settings),
            default_set(),
            pause=GROQ_PAUSE if groq else 0.0,
            retry_wait=GROQ_RETRY_WAIT if groq else 0.0,
            retries=GROQ_RETRIES if groq else 0,
        )
    write(answers.model_dump(mode="json"), output)
    print("\n".join(lines(answers.summary)))
    print(f"Written to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
