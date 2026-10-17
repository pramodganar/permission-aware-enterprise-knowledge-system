"""Questions about the company, and the company with the real model, for the model tests.

Shared by the tests of the relevance threshold and of reranking, which were
both measured on these questions (docs/retrieval.md).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.models import User
from app.retrieval.embeddings import Embedder, load_embedder
from app.retrieval.filters import SearchFilters
from app.retrieval.index import FaissVectorIndex, build_index
from app.retrieval.query import process_question
from app.retrieval.reranking import Reranker
from app.retrieval.retriever import Retrieval, retrieve
from scripts.seed import seed

# question: (who asks it, the document that answers it). The person asking
# may read that document; where several may, the one who reads the least.
ANSWERABLE = {
    "How does our deployment pipeline handle rollbacks?": ("grace", "Deployment Runbook"),
    "How many days of annual leave do I get?": ("alice", "Leave Policy"),
    "What is the approval limit for vendor payments?": ("bob", "Vendor Payment Approval Limits"),
    "What was the root cause of the last production outage?": (
        "alice",
        "Payments Outage Postmortem",
    ),
    "What is the process for a performance improvement plan?": (
        "carol",
        "Performance Improvement Plan Process",
    ),
    "What is next year's headcount budget?": ("bob", "Headcount Budget 2027"),
    "What discount can I offer without approval?": ("david", "Discount Approval Policy"),
    "What is the status of the dispute with our supplier?": ("emma", "Supplier Dispute Status"),
    "How are executive bonuses calculated?": ("grace", "Executive Compensation Strategy 2027"),
    "What is our data retention obligation for contracts?": (
        "emma",
        "Contract Data Retention Obligations",
    ),
    "What is the SOP for a warehouse incident?": ("frank", "Warehouse Incident SOP"),
    "What is the company's three-year strategy?": ("grace", "Three-Year Strategy 2027 to 2029"),
    "What are the salary bands for senior engineers?": ("carol", "Salary Bands 2026"),
    "What acquisitions is the company considering?": ("emma", "Project Falcon Acquisition Paper"),
    "Which employees are on a performance improvement plan?": ("carol", "HR Case File 2026-014"),
    "What is the hotel limit when travelling?": ("frank", "Travel and Expense Policy"),
    "Can I accept a gift from a supplier?": ("david", "Code of Conduct"),
    # Worded unlike the documents.
    "How long do I have to submit my receipts after a trip?": (
        "alice",
        "Travel and Expense Policy",
    ),
    "Who signs off a large payment to a supplier?": ("bob", "Vendor Payment Approval Limits"),
    "What happened when payments went down?": ("alice", "Payments Outage Postmortem"),
    "How much parental leave is there?": ("carol", "Leave Policy"),
    "What should I do if someone is hurt in the warehouse?": ("frank", "Warehouse Incident SOP"),
}

# Nothing in the company documents is about these. Asked by Grace, who can
# read the most, so the whole company had the chance to match.
OFF_TOPIC = [
    "What is the capital of France?",
    "Who won the football world cup in 2022?",
    "How do I bake sourdough bread?",
    "What is the weather tomorrow?",
    "Explain quantum entanglement simply.",
    "Who is the CEO's favourite actor?",
    "What is the office wifi password?",
    "When is the company picnic?",
    "How do I reset my laptop password?",
    "What is the share price today?",
    "Tell me a joke.",
]


# Who may read what, written out from docs/personas.md and not taken from the
# code under test: a check that asked can_read would break along with it.
GENERAL = {"Leave Policy", "Code of Conduct", "Travel and Expense Policy"}
DEPARTMENT_DOCUMENTS = {
    "engineering": {"Deployment Runbook", "Payments Outage Postmortem"},
    "finance": {"Vendor Payment Approval Limits", "Headcount Budget 2027"},
    "hr": {"Performance Improvement Plan Process", "Salary Bands 2026"},
    "sales": {"Discount Approval Policy"},
    "legal": {"Contract Data Retention Obligations", "Supplier Dispute Status"},
    "operations": {"Warehouse Incident SOP"},
    "executive": {"Executive Compensation Strategy 2027", "Three-Year Strategy 2027 to 2029"},
}
ALL_DEPARTMENTAL = set().union(*DEPARTMENT_DOCUMENTS.values())
MAY_READ = {
    "alice": GENERAL | DEPARTMENT_DOCUMENTS["engineering"],
    "bob": GENERAL | DEPARTMENT_DOCUMENTS["finance"],
    "carol": GENERAL | DEPARTMENT_DOCUMENTS["hr"] | {"HR Case File 2026-014"},
    "david": GENERAL | DEPARTMENT_DOCUMENTS["sales"],
    "emma": GENERAL | DEPARTMENT_DOCUMENTS["legal"] | {"Project Falcon Acquisition Paper"},
    "frank": GENERAL | DEPARTMENT_DOCUMENTS["operations"],
    "grace": GENERAL | ALL_DEPARTMENTAL | {"Project Falcon Acquisition Paper"},
    "henry": set(),
    "irene": set(),
}

# A fact that is written in one document only, and so must never reach
# anyone who may not read that document.
FACTS = {
    "E-2291": "HR Case File 2026-014",
    "Brightline": "Project Falcon Acquisition Paper",
    "68,000 to 92,000": "Salary Bands 2026",
    "Northwind": "Supplier Dispute Status",
    "1,240": "Headcount Budget 2027",
    "connection pool": "Payments Outage Postmortem",
}


@dataclass
class Company:
    db: Session
    settings: Settings
    embedder: Embedder
    index: FaissVectorIndex

    def ask(
        self,
        name: str,
        question: str,
        *,
        filters: SearchFilters | None = None,
        reranker: Reranker | None = None,
        limit: int | None = None,
        min_score: float | None = None,
    ) -> Retrieval:
        user = self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()
        return retrieve(
            self.db,
            self.index,
            user,
            process_question(question, self.settings, self.embedder),
            limit or self.settings.retrieval_top_k,
            self.settings.retrieval_min_score if min_score is None else min_score,
            filters=filters,
            reranker=reranker,
            candidates=self.settings.rerank_candidates,
        )


@contextmanager
def real_company() -> Iterator[Company]:
    """The seeded company with the real embedding model and default settings."""
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as db:
        seed(db, settings, "model-test-password")
        embedder = load_embedder(settings)
        yield Company(db, settings, embedder, build_index(db, embedder))
    engine.dispose()
