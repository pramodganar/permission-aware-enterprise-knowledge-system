"""Filters narrow the search within what a person may read, and never widen it."""

import logging
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.ingestion.pipeline import delete_document
from app.models import Chunk, Classification, Document, User
from app.retrieval import retriever as retriever_module
from app.retrieval.filters import MAX_DEPARTMENTS, MAX_DOCUMENTS, SearchFilters, narrow_scope
from app.retrieval.index import FaissVectorIndex, Hit, build_index
from app.retrieval.query import process_question
from app.retrieval.retriever import Retrieval, retrieve
from app.retrieval.scope import AccessScope, access_scope
from app.retrieval.search import search_in_scope
from fakes import FakeEmbedder
from logcheck import everything_logged
from scripts.seed import seed

PEOPLE = ["alice", "bob", "carol", "david", "emma", "frank", "grace", "henry", "irene"]
DEPARTMENTS = ["engineering", "finance", "hr", "sales", "legal", "operations", "executive"]
NO_GATE = -1.0
UNKNOWN_DOCUMENT = 999_999


class Company:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.embedder = FakeEmbedder()
        self.index: FaissVectorIndex = build_index(db, self.embedder)
        self.documents = {d.title: d for d in db.scalars(select(Document))}

    def user(self, name: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()

    def scope(self, name: str) -> AccessScope:
        return access_scope(self.db, self.user(name))

    def retrieve(
        self, name: str, question: str, filters: SearchFilters | None, limit: int = 50
    ) -> Retrieval:
        processed = process_question(question, self.settings, self.embedder)
        return retrieve(
            self.db, self.index, self.user(name), processed, limit, NO_GATE, filters=filters
        )

    def matching_chunks(self, filters: SearchFilters) -> set[int]:
        """Every chunk in the company that matches, worked out in Python."""
        found = set()
        for chunk in self.db.scalars(select(Chunk)):
            document = chunk.document
            if filters.departments is not None and (
                document.department.code not in filters.departments
            ):
                continue
            if filters.classifications is not None and (
                document.classification not in filters.classifications
            ):
                continue
            if filters.document_ids is not None and document.id not in filters.document_ids:
                continue
            found.add(chunk.id)
        return found


@pytest.fixture
def acme(db: Session, settings: Settings) -> Company:
    seed(db, settings, "filters-test-password")
    return Company(db, settings)


def filter_cases(acme: Company) -> list[SearchFilters]:
    ids = sorted(document.id for document in acme.documents.values())
    salary = acme.documents["Salary Bands 2026"].id
    return [
        *(SearchFilters(departments=frozenset({code})) for code in DEPARTMENTS),
        SearchFilters(departments=frozenset({"finance", "legal"})),
        SearchFilters(departments=frozenset({"marketing"})),
        *(SearchFilters(classifications=frozenset({c})) for c in Classification),
        SearchFilters(classifications=frozenset({"general", "restricted"})),
        *(SearchFilters(document_ids=frozenset({i})) for i in ids),
        SearchFilters(document_ids=frozenset({UNKNOWN_DOCUMENT})),
        SearchFilters(departments=frozenset({"hr"}), classifications=frozenset({"general"})),
        SearchFilters(
            departments=frozenset({"executive"}), classifications=frozenset({"restricted"})
        ),
        SearchFilters(departments=frozenset({"finance"}), document_ids=frozenset({salary})),
    ]


# --- narrowing, for everyone and every filter -------------------------------------------


@pytest.mark.parametrize("name", PEOPLE)
def test_the_narrowed_scope_is_exactly_the_scope_and_the_filter(acme: Company, name: str) -> None:
    scope = acme.scope(name)
    cases = filter_cases(acme)
    assert len(cases) == 34

    for filters in cases:
        narrowed = narrow_scope(acme.db, scope, filters)

        assert narrowed.chunk_ids == scope.chunk_ids & acme.matching_chunks(filters), filters
        assert narrowed.chunk_ids <= scope.chunk_ids
        assert narrowed.document_ids <= scope.document_ids


def test_a_department_filter_keeps_only_that_department(acme: Company) -> None:
    scope = acme.scope("grace")

    narrowed = narrow_scope(acme.db, scope, SearchFilters(departments=frozenset({"finance"})))

    titles = {d.title for d in acme.documents.values() if d.id in narrowed.document_ids}
    assert titles == {
        "Travel and Expense Policy",
        "Vendor Payment Approval Limits",
        "Headcount Budget 2027",
    }


def test_fields_are_combined_with_and(acme: Company) -> None:
    filters = SearchFilters(departments=frozenset({"hr"}), classifications=frozenset({"general"}))

    narrowed = narrow_scope(acme.db, acme.scope("carol"), filters)

    assert narrowed.document_ids == {acme.documents["Leave Policy"].id}


def test_the_narrowed_scope_keeps_who_when_and_permissions(acme: Company) -> None:
    scope = acme.scope("emma")

    narrowed = narrow_scope(acme.db, scope, SearchFilters(classifications=frozenset({"general"})))

    assert (narrowed.user_id, narrowed.decided_at, narrowed.permissions) == (
        scope.user_id,
        scope.decided_at,
        scope.permissions,
    )


@pytest.mark.parametrize("filters", [None, SearchFilters()])
def test_no_filter_leaves_the_scope_as_it_is(acme: Company, filters: SearchFilters | None) -> None:
    scope = acme.scope("bob")

    assert narrow_scope(acme.db, scope, filters) is scope


def test_an_empty_scope_stays_empty(acme: Company) -> None:
    scope = acme.scope("henry")

    narrowed = narrow_scope(acme.db, scope, SearchFilters(departments=frozenset({"hr"})))

    assert narrowed.is_empty


# --- asking for something you may not read ----------------------------------------------


@pytest.mark.parametrize("name", ["alice", "david", "emma"])
def test_filtering_by_a_document_you_may_not_read_looks_like_one_that_does_not_exist(
    acme: Company, name: str
) -> None:
    scope = acme.scope(name)
    question = "Which employees are on a performance improvement plan?"
    missing = acme.retrieve(
        name, question, SearchFilters(document_ids=frozenset({UNKNOWN_DOCUMENT}))
    )

    unreadable = [d for d in acme.documents.values() if d.id not in scope.document_ids]
    assert unreadable
    for document in unreadable:
        forbidden = acme.retrieve(
            name, question, SearchFilters(document_ids=frozenset({document.id}))
        )
        assert forbidden == missing
        assert forbidden.is_empty


def test_filtering_by_a_department_you_may_not_read_looks_like_one_that_does_not_exist(
    acme: Company,
) -> None:
    question = "What is next year's headcount budget?"

    finance = acme.retrieve("david", question, SearchFilters(departments=frozenset({"finance"})))
    marketing = acme.retrieve(
        "david", question, SearchFilters(departments=frozenset({"marketing"}))
    )

    # David reads the finance department's general travel policy, nothing else of theirs.
    assert {c.document_title for c in finance.chunks} == {"Travel and Expense Policy"}
    restricted = SearchFilters(departments=frozenset({"executive"}))
    assert acme.retrieve("david", question, restricted) == marketing
    assert marketing.is_empty


def test_asking_for_restricted_documents_gives_only_your_own_grants(acme: Company) -> None:
    restricted = SearchFilters(classifications=frozenset({"restricted"}))

    carol = acme.retrieve("carol", "performance plan", restricted)
    grace = acme.retrieve("grace", "performance plan", restricted)
    david = acme.retrieve("david", "performance plan", restricted)

    assert {c.document_title for c in carol.chunks} == {"HR Case File 2026-014"}
    assert {c.document_title for c in grace.chunks} == {"Project Falcon Acquisition Paper"}
    assert david.is_empty


def test_a_filter_cannot_bring_back_a_deleted_document(acme: Company) -> None:
    runbook = acme.documents["Deployment Runbook"]
    delete_document(acme.db, acme.user("henry"), runbook.id)

    retrieval = acme.retrieve(
        "alice", "rollbacks", SearchFilters(document_ids=frozenset({runbook.id}))
    )

    assert retrieval.is_empty


# --- through the retriever --------------------------------------------------------------


def test_the_retriever_only_returns_chunks_that_match(acme: Company) -> None:
    filters = SearchFilters(departments=frozenset({"legal"}))

    retrieval = acme.retrieve("emma", "supplier", filters)

    assert retrieval.chunks
    assert {c.document_title for c in retrieval.chunks} <= {
        "Code of Conduct",
        "Contract Data Retention Obligations",
        "Supplier Dispute Status",
    }
    assert retrieval.dropped == 0


def test_a_filter_gives_a_narrow_reader_a_full_set_of_results(acme: Company) -> None:
    # Grace asks about bonuses but only wants HR documents. The executive
    # paper is closer, and still doesn't take any of the five places.
    filters = SearchFilters(departments=frozenset({"hr"}))

    retrieval = acme.retrieve("grace", "How are executive bonuses calculated?", filters, limit=5)

    assert len(retrieval.chunks) == 5
    assert all(c.document_title != "Executive Compensation Strategy 2027" for c in retrieval.chunks)


# --- the filter as input ----------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"departments": []},
        {"classifications": []},
        {"document_ids": []},
        {"departments": ["HR; DROP TABLE"]},
        {"departments": ["9finance"]},
        # A bare string, not a list. Must not become the letters h and r.
        {"departments": "hr"},
        {"departments": ["x" * 51]},
        {"departments": [f"d{i}" for i in range(MAX_DEPARTMENTS + 1)]},
        {"classifications": ["secret"]},
        {"document_ids": [0]},
        {"document_ids": [-3]},
        {"document_ids": ["seven"]},
        {"document_ids": list(range(1, MAX_DOCUMENTS + 2))},
        {"user_id": 7},
        {"role": "executive"},
    ],
)
def test_a_bad_filter_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SearchFilters.model_validate(fields)


def test_department_codes_are_normalised_like_the_department_table() -> None:
    filters = SearchFilters.model_validate({"departments": ["  HR ", "Finance"]})

    assert filters.departments == frozenset({"hr", "finance"})


def test_the_limits_themselves_are_allowed() -> None:
    SearchFilters.model_validate(
        {
            "departments": [f"d{i}" for i in range(MAX_DEPARTMENTS)],
            "document_ids": list(range(1, MAX_DOCUMENTS + 1)),
        }
    )


def test_a_filter_cannot_be_changed() -> None:
    filters = SearchFilters(departments=frozenset({"hr"}))

    with pytest.raises(ValidationError):
        filters.departments = frozenset({"executive"})  # type: ignore[misc]


def test_the_filter_has_no_say_in_who_is_asking() -> None:
    # Nothing about the person can come in through a filter.
    assert set(SearchFilters.model_fields) == {"departments", "classifications", "document_ids"}


# --- logging ----------------------------------------------------------------------------


def test_the_kind_of_filter_is_logged_but_not_its_values(
    acme: Company, caplog: pytest.LogCaptureFixture
) -> None:
    case_file = acme.documents["HR Case File 2026-014"]
    filters = SearchFilters(departments=frozenset({"hr"}), document_ids=frozenset({case_file.id}))

    with caplog.at_level(logging.INFO):
        narrow_scope(acme.db, acme.scope("carol"), filters)

    record = next(r for r in caplog.records if r.message == "scope narrowed")
    assert record.filters == ["departments", "document_ids"]  # type: ignore[attr-defined]
    assert record.chunks_after < record.chunks_before  # type: ignore[attr-defined]
    logged = everything_logged(caplog)
    assert "'hr'" not in logged
    # A document ID is a small number that could turn up anywhere in the
    # text, so the fields themselves are checked for the filter's values.
    for value in (v for r in caplog.records for v in vars(r).values()):
        if isinstance(value, list | set | frozenset | tuple):
            assert case_file.id not in value
            assert "hr" not in value
    assert "Case File" not in logged


# --- each of the two guards on its own --------------------------------------------------
# Narrowing has two guards against widening: the query only looks at the
# scope's documents, and the result is intersected with the scope. Either one
# alone is enough, so a test of the outcome can't tell if one is missing.
# These test each guard by taking the other away.


class RecordingSession:
    """A session that passes queries through and keeps what they returned."""

    def __init__(self, db: Session, replace_rows: list[Any] | None = None) -> None:
        self.db = db
        self.replace_rows = replace_rows
        self.returned: list[Any] = []

    def execute(self, query: Any) -> Any:
        result = self.db.execute(query)
        rows = list(result.all()) if self.replace_rows is None else self.replace_rows
        self.returned.extend(rows)

        class Rows:
            def all(self) -> list[Any]:
                return rows

        return Rows()


def test_the_intersection_alone_stops_a_query_that_returns_too_much(acme: Company) -> None:
    # As if the query were wrong and returned every chunk in the company.
    scope = acme.scope("david")
    everything = list(acme.db.execute(select(Chunk.id, Chunk.document_id)).all())
    faulty = RecordingSession(acme.db, replace_rows=everything)

    narrowed = narrow_scope(
        faulty,  # type: ignore[arg-type]
        scope,
        SearchFilters(classifications=frozenset({"general", "departmental", "restricted"})),
    )

    assert narrowed.chunk_ids == scope.chunk_ids
    assert narrowed.document_ids == scope.document_ids


@pytest.mark.parametrize("name", ["david", "carol", "henry"])
def test_the_query_alone_only_looks_at_the_scope(acme: Company, name: str) -> None:
    scope = acme.scope(name)
    recording = RecordingSession(acme.db)

    for filters in filter_cases(acme):
        narrow_scope(recording, scope, filters)  # type: ignore[arg-type]

    assert all(row[1] in scope.document_ids for row in recording.returned)
    if not scope.is_empty:
        assert recording.returned


# --- filters and the relevance gate -----------------------------------------------------
# The gate is judged on the best chunk the person may read, before the filter.
# Otherwise a filter that removes the strong chunk leaves the one the person
# asked for just under the threshold, which is what happened to Carol with the
# real model (docs/retrieval.md).

PLAN_QUESTION = "Which employees are on a performance improvement plan?"
RESTRICTED = SearchFilters(classifications=frozenset({"restricted"}))


def gated(acme: Company, name: str, filters: SearchFilters | None, min_score: float) -> Retrieval:
    processed = process_question(PLAN_QUESTION, acme.settings, acme.embedder)
    return retrieve(acme.db, acme.index, acme.user(name), processed, 5, min_score, filters=filters)


def test_a_filter_does_not_push_a_relevant_question_under_the_threshold(acme: Company) -> None:
    unfiltered = acme.retrieve("carol", PLAN_QUESTION, None)
    filtered = acme.retrieve("carol", PLAN_QUESTION, RESTRICTED)
    best_overall, best_filtered = unfiltered.chunks[0].score, filtered.chunks[0].score
    assert best_filtered < best_overall
    threshold = (best_overall + best_filtered) / 2

    retrieval = gated(acme, "carol", RESTRICTED, threshold)

    assert not retrieval.below_threshold
    assert {c.document_title for c in retrieval.chunks} == {"HR Case File 2026-014"}
    assert retrieval.best_score == best_overall


def test_with_a_filter_the_gate_still_stops_an_irrelevant_question(acme: Company) -> None:
    best_overall = acme.retrieve("carol", PLAN_QUESTION, None).chunks[0].score

    retrieval = gated(acme, "carol", RESTRICTED, best_overall + 0.01)

    assert retrieval.below_threshold
    assert retrieval.is_empty


def test_a_filter_that_leaves_nothing_gives_nothing(acme: Company) -> None:
    executive = SearchFilters(departments=frozenset({"executive"}))

    retrieval = gated(acme, "david", executive, NO_GATE)

    assert retrieval.is_empty
    assert retrieval.best_score is None


def test_the_wider_gate_search_stays_inside_what_the_person_may_read(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    scopes: list[AccessScope] = []

    def recording(*arguments: Any, **keywords: Any) -> Any:
        scopes.append(arguments[1])
        return search_in_scope(*arguments, **keywords)

    monkeypatch.setattr(retriever_module, "search_in_scope", recording)
    readable = acme.scope("carol")

    gated(acme, "carol", RESTRICTED, NO_GATE)

    assert len(scopes) == 2
    filtered, wider = scopes
    assert filtered.chunk_ids < readable.chunk_ids
    assert wider.chunk_ids == readable.chunk_ids


def test_without_a_filter_there_is_one_search(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def recording(*arguments: Any, **keywords: Any) -> Any:
        calls.append(arguments)
        return search_in_scope(*arguments, **keywords)

    monkeypatch.setattr(retriever_module, "search_in_scope", recording)

    gated(acme, "carol", None, NO_GATE)

    assert len(calls) == 1


def test_a_forbidden_chunk_in_the_wider_search_does_not_open_the_gate(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    falcon = acme.documents["Project Falcon Acquisition Paper"].chunks[0].id

    def wider_search_with_a_fault(*arguments: Any, **keywords: Any) -> Any:
        if arguments[3] == 1:  # the gate's search for the single best chunk
            return [Hit(chunk_id=falcon, score=0.99)]
        return search_in_scope(*arguments, **keywords)

    monkeypatch.setattr(retriever_module, "search_in_scope", wider_search_with_a_fault)
    filtered_best = acme.retrieve("carol", PLAN_QUESTION, RESTRICTED).chunks[0].score

    retrieval = gated(acme, "carol", RESTRICTED, filtered_best + 0.01)

    # Carol may not read the acquisition paper, so its 0.99 counts for nothing.
    assert retrieval.dropped == 1
    assert retrieval.below_threshold
    assert retrieval.best_score == filtered_best
