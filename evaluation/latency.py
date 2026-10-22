"""How long a question takes, stage by stage, and what the permission filter costs.

    python -m evaluation.latency                  # no model: the service's own work
    python -m evaluation.latency --rerank         # the same with the cross-encoder
    python -m evaluation.latency --model groq     # with the hosted model, end to end

Every question in the set is asked by its asker through ``answer_question``,
the service's own path, over a freshly loaded company with the real
embedding model. The stages are timed by wrapping the functions that path
calls while the measurement runs, so what is timed is the service's code
and not a copy of it:

    embed           checking the question and embedding it
    scope           working out what the asker may read (SQL)
    search          the vector search inside that scope
    load_and_check  loading the chunks found and checking each with can_read
    rerank          the cross-encoder, when it is on
    context         joining the chunks into sources
    model           the language model, with its retries
    total           the whole of answer_question, audit records included

What the permission filter costs (NFR-2) is the work an unfiltered system
wouldn't do: the scope, the can_read checks, and the difference between
searching inside the scope and searching every vector. The last is timed by
a search with no allowed list, on the same vector, next to each question.
That search exists only here: the service has no way to search without one.

Without a model, the first round warms up and is left out, and the rest are
the measurement. With Groq, each question is asked once, paced to its rate
limit; the pause is not part of any time.

Times depend on the machine and on what else it is doing, so a run is a
record, not a baseline: the results say which machine they came from.
"""

import argparse
import math
import os
import platform
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import insert, select

import app.rag.answer
import app.retrieval.retriever
from app.models import Chunk
from app.rag.answer import answer_question
from app.rag.llm import LLMError, LLMProvider
from app.retrieval.index import FaissVectorIndex
from app.retrieval.query import ProcessedQuestion
from app.retrieval.reranking import Reranker, load_reranker
from app.retrieval.scope import access_scope
from app.retrieval.search import search_in_scope
from evaluation.company import Company, loaded_company
from evaluation.dataset import PEOPLE, QuestionSet, default_set
from evaluation.models import ModelChoice, provider_for
from evaluation.retrieval import RERANKER_MODEL, RESULTS, write
from evaluation.security import GROQ_PAUSE, settings_for

ROUNDS = 5

# Each stage, and the function the service calls for it, in the module that calls it.
STAGES: dict[str, tuple[Any, str]] = {
    "embed": (app.rag.answer, "process_question"),
    "scope": (app.retrieval.retriever, "access_scope"),
    "search": (app.retrieval.retriever, "search_in_scope"),
    "load_and_check": (app.retrieval.retriever, "_load_and_check"),
    "can_read": (app.retrieval.retriever, "can_read"),
    "rerank": (app.retrieval.retriever, "_reranked"),
    "context": (app.rag.answer, "build_context"),
    "model": (app.rag.answer, "complete"),
}


@dataclass
class Clock:
    """Seconds spent in each stage during one question, and what the stages returned."""

    spent: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    returned: dict[str, Any] = field(default_factory=dict)

    def timed(self, stage: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                result = function(*args, **kwargs)
            finally:
                self.spent[stage] += time.perf_counter() - started
            self.returned[stage] = result
            return result

        return wrapper


@contextmanager
def timing(clock: Clock) -> Iterator[None]:
    """Time every stage while inside, and put the service's functions back after."""
    originals = {stage: getattr(module, name) for stage, (module, name) in STAGES.items()}
    try:
        for stage, (module, name) in STAGES.items():
            setattr(module, name, clock.timed(stage, originals[stage]))
        yield
    finally:
        for stage, (module, name) in STAGES.items():
            setattr(module, name, originals[stage])


def unfiltered_search(company: Company, vector: Any, limit: int) -> float:
    """Seconds to search every vector with no allowed list: what no filter would cost."""
    return unfiltered_search_of(company.index, vector, limit)


def unfiltered_search_of(index: FaissVectorIndex, vector: Any, limit: int) -> float:
    # The index's own search always takes an allowed list. Reaching past it is
    # done here and nowhere in the service.
    query = np.ascontiguousarray(vector.reshape(1, -1), dtype=np.float32)
    started = time.perf_counter()
    index._index.search(query, limit)
    return time.perf_counter() - started


@dataclass(frozen=True)
class Timed:
    question_id: str
    # Seconds, by stage. A stage that didn't run isn't there.
    stages: dict[str, float]
    total: float
    unfiltered_search: float
    answered: bool
    reached_model: bool
    failed: str | None = None

    @property
    def filter_cost(self) -> float:
        """The scope, the can_read checks, and searching inside the scope over searching all."""
        searching = self.stages.get("search", 0.0) - self.unfiltered_search
        return self.stages.get("scope", 0.0) + self.stages.get("can_read", 0.0) + searching


def time_question(
    company: Company,
    provider: LLMProvider,
    question_id: str,
    asker: str,
    question: str,
    *,
    reranker: Reranker | None = None,
) -> Timed:
    clock = Clock()
    person = company.person(asker)
    failed = None
    answered = False
    started = time.perf_counter()
    with timing(clock):
        try:
            answer = answer_question(
                company.db,
                company.index,
                company.embedder,
                provider,
                person,
                question,
                company.settings,
                reranker=reranker,
            )
            answered = answer.answered
        except LLMError as error:
            failed = type(error).__name__
    total = time.perf_counter() - started
    vector = clock.returned["embed"].vector
    limit = (
        company.settings.retrieval_top_k if reranker is None else company.settings.rerank_candidates
    )
    return Timed(
        question_id=question_id,
        stages=dict(clock.spent),
        total=total,
        unfiltered_search=unfiltered_search(company, vector, limit),
        answered=answered,
        reached_model="model" in clock.spent,
        failed=failed,
    )


def run(
    company: Company,
    provider: LLMProvider,
    questions: QuestionSet,
    *,
    rounds: int = ROUNDS,
    warm_up: bool = True,
    reranker: Reranker | None = None,
    pause: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Timed]:
    """Every question by its asker, ``rounds`` times, after a round that is left out."""
    measured: list[Timed] = []
    for round_number in range(rounds + int(warm_up)):
        for q in questions.questions:
            if pause:
                sleep(pause)
            timed = time_question(company, provider, q.id, q.asker, q.question, reranker=reranker)
            if round_number >= int(warm_up):
                measured.append(timed)
    return measured


def percentile(values: list[float], p: float) -> float:
    """The nearest-rank percentile: the smallest value with p percent of them at or below it."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def milliseconds(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50": round(percentile(values, 50) * 1000, 1),
        "p95": round(percentile(values, 95) * 1000, 1),
        "max": round(max(values) * 1000, 1),
        "mean": round(sum(values) / len(values) * 1000, 1),
    }


def summary(timings: list[Timed]) -> dict[str, Any]:
    ok = [t for t in timings if t.failed is None]
    stages: dict[str, Any] = {}
    for stage in [*STAGES, "unfiltered_search", "filter_cost", "retrieval", "total"]:
        if stage == "unfiltered_search":
            values = [t.unfiltered_search for t in ok]
        elif stage == "filter_cost":
            values = [t.filter_cost for t in ok]
        elif stage == "retrieval":
            # The retriever's own stages: what NFR-1's retrieval target is about.
            values = [
                sum(t.stages.get(s, 0.0) for s in ("scope", "search", "load_and_check", "rerank"))
                for t in ok
            ]
        elif stage == "total":
            values = [t.total for t in ok]
        else:
            values = [t.stages[stage] for t in ok if stage in t.stages]
        if values:
            stages[stage] = milliseconds(values)
    reached = [t for t in ok if t.reached_model]
    return {
        "timings": len(timings),
        "failed": sorted({t.question_id for t in timings if t.failed is not None}),
        "reached_model": len(reached),
        "stages_ms": stages,
        # Of the whole question, how much is the permission filter.
        "filter_share_of_total_p50": round(percentile([t.filter_cost / t.total for t in ok], 50), 4)
        if ok
        else None,
        "end_to_end_answered_ms": milliseconds([t.total for t in reached]) if reached else None,
    }


def machine() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
    }


def report(
    timings: list[Timed], company: Company, *, choice: ModelChoice, reranked: bool, rounds: int
) -> dict[str, Any]:
    return {
        "model": choice,
        "reranked": reranked,
        "rounds": rounds,
        "settings": {
            "embedding_model": company.settings.embedding_model,
            "reranker_model": company.settings.reranker_model,
            "top_k": company.settings.retrieval_top_k,
            "rerank_candidates": company.settings.rerank_candidates,
            "chunks_indexed": company.index.size,
        },
        "machine": machine(),
        **summary(timings),
    }


def lines(numbers: dict[str, Any]) -> list[str]:
    out = [
        f"Model: {numbers['model']}, reranked: {numbers['reranked']}, "
        f"{numbers['timings']} timings, {numbers['reached_model']} reached the model"
    ]
    for stage, m in numbers["stages_ms"].items():
        out.append(f"{stage:>18}: p50 {m['p50']:>8} ms  p95 {m['p95']:>8} ms  (n={m['count']})")
    out.append(f"Filter share of the whole question (p50): {numbers['filter_share_of_total_p50']}")
    out.append(f"Failed: {', '.join(numbers['failed']) or 'none'}")
    return out


SCALE_REPEATS = 20


def scaled(company: Company, copies: int, seed: int = 0) -> FaissVectorIndex:
    """Make every chunk ``copies`` times over, and index them all with random vectors.

    The copies belong to the same documents, so who may read what is the
    company's own rule, at a larger size. What a search finds doesn't matter
    here, only how long it takes, so the vectors are random.
    """
    chunks = company.db.scalars(select(Chunk)).all()
    company.db.execute(
        insert(Chunk),
        [
            {
                "document_id": c.document_id,
                # Clear of every real position, and of each other copy's.
                "position": c.position + copy * POSITION_STEP,
                "start_offset": c.start_offset,
                "end_offset": c.end_offset,
                "text": c.text,
            }
            for copy in range(1, copies)
            for c in chunks
        ],
    )
    company.db.commit()
    ids = list(company.db.scalars(select(Chunk.id).order_by(Chunk.id)))
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((len(ids), company.index.dimension)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    index = FaissVectorIndex(company.index.dimension)
    index.add(ids, vectors)
    return index


# More than any document has chunks, so a copy's position never meets another's.
POSITION_STEP = 10_000


def at_scale(
    company: Company, index: FaissVectorIndex, *, repeats: int = SCALE_REPEATS, seed: int = 1
) -> dict[str, Any]:
    """Each reader's scope and scoped search on the larger index, beside a search of everything."""
    rng = np.random.default_rng(seed)
    limit = company.settings.retrieval_top_k
    people: dict[str, Any] = {}
    for name in PEOPLE:
        person = company.person(name)
        vector = rng.standard_normal(index.dimension).astype(np.float32)
        vector /= np.linalg.norm(vector)
        question = ProcessedQuestion(text="scale", vector=vector, tokens=1)
        scope_times, search_times, plain_times = [], [], []
        chunks_in_scope = 0
        for _ in range(repeats):
            started = time.perf_counter()
            scope = access_scope(company.db, person)
            scope_times.append(time.perf_counter() - started)
            chunks_in_scope = len(scope.chunk_ids)
            started = time.perf_counter()
            search_in_scope(index, scope, question, limit)
            search_times.append(time.perf_counter() - started)
            plain_times.append(unfiltered_search_of(index, vector, limit))
        people[name] = {
            "chunks_in_scope": chunks_in_scope,
            "scope_ms": milliseconds(scope_times),
            "search_ms": milliseconds(search_times),
            "unfiltered_search_ms": milliseconds(plain_times),
        }
    return {"chunks_indexed": index.size, "repeats": repeats, "people": people}


def scale_lines(numbers: dict[str, Any]) -> list[str]:
    out = [f"{numbers['chunks_indexed']} chunks indexed"]
    for name, p in numbers["people"].items():
        out.append(
            f"{name:>6}: {p['chunks_in_scope']:>6} in scope, scope p50 {p['scope_ms']['p50']} ms, "
            f"search p50 {p['search_ms']['p50']} ms, "
            f"unfiltered p50 {p['unfiltered_search_ms']['p50']} ms"
        )
    return out


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure latency by stage.")
    parser.add_argument("--model", choices=["worst", "groq"], default="worst")
    parser.add_argument("--rerank", action="store_true", help="rerank with the cross-encoder")
    parser.add_argument("--rounds", type=int, default=ROUNDS)
    parser.add_argument(
        "--scale",
        type=int,
        metavar="COPIES",
        help="instead, copy every chunk this many times and time the scope and search alone",
    )
    parser.add_argument("--output", type=Path, help="where to write the results (JSON)")
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    if arguments.scale:
        output = arguments.output or RESULTS / "latency-scale.json"
        with loaded_company(settings_for("worst")) as company:
            numbers = {"copies": arguments.scale, "machine": machine()} | at_scale(
                company, scaled(company, arguments.scale)
            )
        write(numbers, output)
        print("\n".join(scale_lines(numbers)))
        print(f"Written to {output}")
        return 0

    choice: ModelChoice = arguments.model
    groq = choice == "groq"
    name = f"latency-{choice}{'-reranked' if arguments.rerank else ''}.json"
    output = arguments.output or RESULTS / name
    settings = settings_for(choice)
    if arguments.rerank:
        settings = settings.model_copy(update={"reranker_model": RERANKER_MODEL})
    with loaded_company(settings) as company:
        reranker = load_reranker(company.settings) if arguments.rerank else None
        rounds = 1 if groq else arguments.rounds
        timings = run(
            company,
            provider_for(choice, company.settings),
            default_set(),
            rounds=rounds,
            # One round with a hosted model, paced: nothing to warm up but its quota.
            warm_up=not groq,
            reranker=reranker,
            pause=GROQ_PAUSE if groq else 0.0,
        )
        numbers = report(timings, company, choice=choice, reranked=arguments.rerank, rounds=rounds)
    write(numbers, output)
    print("\n".join(lines(numbers)))
    print(f"Written to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
