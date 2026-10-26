"""Loading a model draws no progress bar (docs/logging.md).

The model libraries draw one on stderr while a model loads, and in a
container's log it was text among the JSON on every start. The real
libraries take many seconds to import, so a stand-in takes their place and
records whether the bars were off at the moment a model was built.
"""

import sys
import types
from collections.abc import Callable, Iterator
from typing import ClassVar

import pytest
from huggingface_hub.utils.tqdm import are_progress_bars_disabled, enable_progress_bars
from transformers.utils import logging as transformers_logging

from app.retrieval.embeddings import SentenceTransformerEmbedder
from app.retrieval.reranking import CrossEncoderReranker


class Model:
    """Builds like a sentence-transformers model, and notes the bars when it does."""

    seen: ClassVar[list[tuple[bool, bool]]] = []
    max_seq_length = 128

    def __init__(self, *args: object, **kwargs: object) -> None:
        Model.seen.append(
            (are_progress_bars_disabled(), transformers_logging.is_progress_bar_enabled())
        )

    def get_embedding_dimension(self) -> int:
        return 8


@pytest.fixture
def bars_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    stub = types.ModuleType("sentence_transformers")
    setattr(stub, "SentenceTransformer", Model)  # noqa: B010 - a module made here
    setattr(stub, "CrossEncoder", Model)  # noqa: B010
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)
    enable_progress_bars()
    transformers_logging.enable_progress_bar()  # type: ignore[no-untyped-call]  # unannotated
    Model.seen.clear()
    yield
    # The libraries' own default.
    enable_progress_bars()
    transformers_logging.enable_progress_bar()  # type: ignore[no-untyped-call]  # unannotated


@pytest.mark.parametrize(
    "load",
    [lambda: SentenceTransformerEmbedder("model"), lambda: CrossEncoderReranker("model")],
    ids=["embedder", "reranker"],
)
def test_no_bar_is_drawn_while_a_model_loads(bars_on: None, load: Callable[[], object]) -> None:
    load()

    # (Hugging Face's bars off, transformers' bars on): both switched off first.
    assert Model.seen == [(True, False)]
