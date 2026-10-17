"""The demo behind the Streamlit page: the AcmeCorp company, ready to answer.

Everything is built in memory when the page starts: the database is seeded
with the fictional company, the chunks are embedded, the index is built. Then
any of the nine people can be picked and asked a question.

Picking a person stands in for logging in. In the service, who is asking
comes from a signed token (decision record 0004); here the visitor chooses,
because the point of the demo is to compare what different people get.
Everything after that is the real code: the same ``answer_question``, the
same access checks, the same prompt.

Two things are added for a page anyone with the link can open:

- Answers are kept per person and question, so asking the same thing twice
  doesn't call the model again. The key includes the person, so one person's
  answer is never shown to another (threat T6).
- One question is answered at a time. The in-memory database is one SQLite
  connection, and the page serves each visitor on its own thread.
"""

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.service import readable_documents
from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.models import Document, User
from app.rag.answer import Answer, answer_question
from app.rag.context import Context, build_context
from app.rag.llm import LLMProvider
from app.retrieval.embeddings import Embedder, load_embedder
from app.retrieval.index import FaissVectorIndex, build_index
from app.retrieval.query import process_question
from app.retrieval.retriever import retrieve
from scripts.seed import seed

# Kept answers, oldest dropped first.
MAX_KEPT_ANSWERS = 500


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    department: str
    roles: tuple[str, ...]
    # Titles of the documents they may read, in alphabetical order.
    reads: tuple[str, ...]


class Demo:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        embedder: Embedder,
        index: FaissVectorIndex,
        provider: LLMProvider | None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.embedder = embedder
        self.index = index
        self.provider = provider
        self._lock = threading.Lock()
        self._kept: OrderedDict[tuple[str, str], Answer] = OrderedDict()

    @property
    def answers_on(self) -> bool:
        return self.provider is not None

    def people(self) -> list[Person]:
        with self._lock:
            users = self.db.scalars(select(User).order_by(User.email)).all()
            return [self._person(user) for user in users]

    def ask(self, key: str, question: str) -> Answer:
        """Answer the question as this person. Needs a model."""
        if self.provider is None:
            raise RuntimeError("no language model is configured, so there are no answers")
        kept_as = (key, question.strip())
        with self._lock:
            if kept_as in self._kept:
                self._kept.move_to_end(kept_as)
                return self._kept[kept_as]
            answer = answer_question(
                self.db,
                self.index,
                self.embedder,
                self.provider,
                self._user(key),
                question,
                self.settings,
            )
            self._kept[kept_as] = answer
            if len(self._kept) > MAX_KEPT_ANSWERS:
                self._kept.popitem(last=False)
            return answer

    def search(self, key: str, question: str) -> Context:
        """The passages this person would get, without asking a model."""
        with self._lock:
            found = retrieve(
                self.db,
                self.index,
                self._user(key),
                process_question(question, self.settings, self.embedder),
                self.settings.retrieval_top_k,
                self.settings.retrieval_min_score,
            )
            return build_context(found, self.settings.context_max_characters)

    def _user(self, key: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{key}@acmecorp.example")).one()

    def _person(self, user: User) -> Person:
        titles = self.db.scalars(readable_documents(user).with_only_columns(Document.title))
        return Person(
            key=user.email.split("@")[0],
            name=user.full_name,
            department=user.department.name,
            roles=tuple(sorted(role.code for role in user.roles)),
            reads=tuple(sorted(titles)),
        )


def build_demo(
    settings: Settings,
    provider: LLMProvider | None,
    embedder: Embedder | None = None,
) -> Demo:
    """Load the company into a new in-memory database and index it. Slow: do it once."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = create_session_factory(engine)()
    # Nobody logs in to the demo, so the password is never used or shown.
    seed(db, settings, secrets.token_urlsafe(24))
    embedder = embedder or load_embedder(settings)
    return Demo(db, settings, embedder, build_index(db, embedder), provider)
