"""The decision records stay in one shape, and say nothing untrue about what runs.

Record 0007 said from the start that the Docker Compose setup uses
PostgreSQL, when Compose has only ever run SQLite. A record isn't edited
once accepted, so nothing read it again. These check that the index lists every record as
its heading names it, that every record has the same sections, that a
correction is dated, and that no page says Compose runs something it doesn't.
"""

import re

from docrefs import ROOT, prose

ADR = ROOT / "docs" / "adr"
SECTIONS = ["Context", "Decision", "Alternatives", "Consequences", "Revisit when"]
# Things a page might say a setup runs. A sentence that says Compose runs one
# of them must be about a Compose file that does.
PRODUCTS = ["PostgreSQL", "Postgres", "pgvector", "Redis", "Qdrant", "Weaviate", "Chroma", "Nginx"]
NEGATION = re.compile(r"n't\b|\bnot\b|\bno\b|\bnever\b", re.IGNORECASE)
MONTH = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
    r" \d{4}\. ",
    re.MULTILINE,
)


def records() -> list[str]:
    return sorted(p.name for p in ADR.glob("[0-9][0-9][0-9][0-9]-*.md"))


def test_the_index_lists_every_record_as_its_heading_names_it() -> None:
    index = (ADR / "README.md").read_text(encoding="utf-8")
    listed = re.findall(r"^\| \[(\d{4})\]\((\S+)\) \| (.+?) \| (\w+) \|$", index, re.MULTILINE)

    assert [file for _, file, _, _ in listed] == records()
    for number, file, title, status in listed:
        text = (ADR / file).read_text(encoding="utf-8")
        assert text.startswith(f"# {number}. {title}\n"), file
        assert f"\nStatus: {status}\n" in text, file


def test_every_record_has_its_sections_in_order() -> None:
    for file in records():
        headings = re.findall(r"^## (.+)$", (ADR / file).read_text(encoding="utf-8"), re.MULTILINE)

        assert headings[: len(SECTIONS)] == SECTIONS, file
        # After them only what happened later: a revisit or a correction.
        later = headings[len(SECTIONS) :]
        assert all(h == "Corrections" or h.startswith("Revisited ") for h in later), file


def test_a_correction_is_dated() -> None:
    corrected = 0
    for file in records():
        text = (ADR / file).read_text(encoding="utf-8")
        if "\n## Corrections\n" in text:
            corrected += 1
            assert MONTH.search(text.split("\n## Corrections\n", 1)[1]), file

    assert corrected >= 1


def test_no_page_says_compose_runs_something_it_does_not() -> None:
    compose = "".join(
        (ROOT / name).read_text(encoding="utf-8").lower()
        for name in ("compose.yaml", "compose.tls.yaml")
    )
    pages = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]

    for page in pages:
        for sentence in re.split(r"(?<=[.!?])\s+", prose(page)):
            if "Compose" not in sentence or NEGATION.search(sentence):
                continue
            for product in PRODUCTS:
                if re.search(rf"\b{product}\b", sentence):
                    assert product.lower() in compose, f"{page.name}: {sentence.strip()}"
