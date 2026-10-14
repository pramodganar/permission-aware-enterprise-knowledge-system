"""A vector search restricted to a user's chunks returns nobody else's.

The index holds every chunk of the company, the restricted ones included.
These tests run each person's questions against it the way the application
does, through the retriever, and look at what comes back.

The embedder is the fake one. What is tested is the restriction, not the
quality of the ranking.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.document_access import revoke_access
from app.authorization.service import can_read
from app.core.config import Settings
from app.models import Chunk, Document, User
from app.retrieval.index import FaissVectorIndex, build_index
from app.retrieval.query import process_question
from app.retrieval.retriever import retrieve
from fakes import FakeEmbedder
from scripts.seed import seed

USERS = ["alice", "bob", "carol", "david", "emma", "frank", "grace", "henry", "irene"]

# question: the document that answers it
QUESTIONS = {
    "How does our deployment pipeline handle rollbacks?": "Deployment Runbook",
    "How many days of annual leave do I get?": "Leave Policy",
    "What is the approval limit for vendor payments?": "Vendor Payment Approval Limits",
    "What was the root cause of the last production outage?": "Payments Outage Postmortem",
    "What is the process for a performance improvement plan?": (
        "Performance Improvement Plan Process"
    ),
    "What is next year's headcount budget?": "Headcount Budget 2027",
    "What discount can I offer without approval?": "Discount Approval Policy",
    "What is the status of the dispute with our supplier?": "Supplier Dispute Status",
    "How are executive bonuses calculated?": "Executive Compensation Strategy 2027",
    "What is our data retention obligation for contracts?": "Contract Data Retention Obligations",
    "What is the SOP for a warehouse incident?": "Warehouse Incident SOP",
    "What is the company's three-year strategy?": "Three-Year Strategy 2027 to 2029",
    "What are the salary bands for senior engineers?": "Salary Bands 2026",
    "What acquisitions is the company considering?": "Project Falcon Acquisition Paper",
    "Which employees are on a performance improvement plan?": "HR Case File 2026-014",
    "What is the hotel limit when travelling?": "Travel and Expense Policy",
    "Can I accept a gift from a supplier?": "Code of Conduct",
}


class Company:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.embedder = FakeEmbedder()
        self.index: FaissVectorIndex = build_index(db, self.embedder)
        self.chunks = {chunk.id: chunk for chunk in db.scalars(select(Chunk))}
        # Hits the second check removed, over every search in a test.
        self.dropped = 0

    def user(self, name: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()

    def search(self, name: str, question: str, limit: int = 10) -> list[Chunk]:
        # The same path a question takes in the application.
        processed = process_question(question, self.settings, self.embedder)
        # No relevance gate: the fake embedder's scores mean nothing, and
        # what is tested here is access, not relevance.
        retrieval = retrieve(self.db, self.index, self.user(name), processed, limit, -1.0)
        self.dropped += retrieval.dropped
        return [self.chunks[found.chunk_id] for found in retrieval.chunks]


@pytest.fixture
def acme(db: Session, settings: Settings) -> Company:
    seed(db, settings, "restricted-search-password")
    return Company(db, settings)


def test_the_index_holds_the_whole_company_including_restricted_documents(acme: Company) -> None:
    titles = {chunk.document.title for chunk in acme.chunks.values()}

    assert acme.index.size == 57
    assert {"HR Case File 2026-014", "Project Falcon Acquisition Paper"} <= titles


@pytest.mark.parametrize("name", USERS)
def test_every_result_is_a_chunk_the_person_may_read(acme: Company, name: str) -> None:
    user = acme.user(name)

    for question in QUESTIONS:
        for chunk in acme.search(name, question):
            assert can_read(user, chunk.document), (name, question, chunk.document.title)


def test_no_search_returns_a_chunk_of_a_document_the_person_may_not_read(acme: Company) -> None:
    forbidden_searches = leaks = 0

    for name in USERS:
        user = acme.user(name)
        for question, title in QUESTIONS.items():
            document = acme.db.scalars(select(Document).where(Document.title == title)).one()
            if can_read(user, document):
                continue
            forbidden_searches += 1
            leaks += any(chunk.document_id == document.id for chunk in acme.search(name, question))

    # 9 people x 17 questions = 153 searches, of which this many ask for a
    # document the person may not read.
    assert forbidden_searches == 107
    assert leaks == 0


def test_the_scope_and_the_second_check_agree_on_every_search(acme: Company) -> None:
    # The scope is a SQL query and the second check is the same rule in
    # Python. Over every person and question they must never disagree.
    searches = 0
    for name in USERS:
        for question in QUESTIONS:
            acme.search(name, question, limit=50)
            searches += 1

    assert searches == 153
    assert acme.dropped == 0


@pytest.mark.parametrize("name", ["henry", "irene"])
def test_someone_with_no_reading_role_finds_nothing(acme: Company, name: str) -> None:
    for question in QUESTIONS:
        assert acme.search(name, question) == []


def test_the_bonus_question_from_the_stopped_pilot(acme: Company) -> None:
    question = "How are executive bonuses calculated?"
    paper = "Executive Compensation Strategy 2027"

    david = [chunk.document.title for chunk in acme.search("david", question)]
    grace = [chunk.document.title for chunk in acme.search("grace", question)]

    assert paper not in david
    assert paper in grace


def test_an_executive_does_not_find_the_case_file(acme: Company) -> None:
    question = "Which employees are on a performance improvement plan?"

    grace = {chunk.document.title for chunk in acme.search("grace", question, limit=50)}
    carol = {chunk.document.title for chunk in acme.search("carol", question, limit=50)}

    assert "HR Case File 2026-014" not in grace
    assert "HR Case File 2026-014" in carol


def test_a_person_still_gets_a_full_set_of_results(acme: Company) -> None:
    # David reads 4 of 17 documents. Chunks he may not read don't take places:
    # he gets five results, all his own.
    chunks = acme.search("david", "How are executive bonuses calculated?", limit=5)

    assert len(chunks) == 5
    assert all(can_read(acme.user("david"), chunk.document) for chunk in chunks)


def test_a_revoked_grant_stops_the_search_at_once_without_rebuilding(acme: Company) -> None:
    question = "Which employees are on a performance improvement plan?"
    case_file = acme.db.scalars(
        select(Document).where(Document.title == "HR Case File 2026-014")
    ).one()
    size_before = acme.index.size
    assert any(chunk.document_id == case_file.id for chunk in acme.search("carol", question, 50))

    revoke_access(acme.db, acme.user("henry"), case_file.id, "carol@acmecorp.example")

    assert not any(
        chunk.document_id == case_file.id for chunk in acme.search("carol", question, 50)
    )
    assert acme.index.size == size_before
