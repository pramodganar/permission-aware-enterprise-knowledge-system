"""The demo scenario shows what the system does, not what I hope it does (docs/demo-scenario.md).

Part A is people asking questions on the demo page with Groq. Each row's
outcome must be the one the evaluation measured: a refused question is a
forbidden one whose probe got no information and leaked nothing, and an
answer is one the evaluation saw given, citing that document, either to the
asker of an answerable question or to the control of a forbidden one. A row
nobody measured fails. The demo page's examples must be exactly these rows.

Part B is the full stack, step by step. Each step is done here through the
API, the way the UI does it, and the page names the test for each step.
"""

import ast
import json
import re
from typing import Any
from uuid import UUID

import pytest
from docrefs import ROOT, check_references
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from apiclient import MISSING, company_client, email, headers
from app.core.config import Settings
from app.models import Document

PAGE = ROOT / "docs" / "demo-scenario.md"
RESULTS = ROOT / "evaluation" / "results"
PAPER = "Project Falcon Acquisition Paper"
NO_INFORMATION = "No information"
_ROW = re.compile(r"^\| (A\d+) \| (\w+) \| ([^|]+?) \| ([^|]+?) \|$", re.MULTILINE)
_SHOWN_BY = re.compile(r"^Shown by (`[^`]+`)\.$", re.MULTILINE)


def load(name: str) -> Any:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def part_a() -> list[tuple[str, str, str, str]]:
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("## Part A", 1)[1].split("\n## ", 1)[0]
    return [
        (step, who.lower(), question, said) for step, who, question, said in _ROW.findall(section)
    ]


def measured(person: str, question: str) -> str | None:
    """What the evaluation saw this person get for this question, in the page's words."""
    labelled = load("evaluation/questions.json")["questions"]
    answers = {a["id"]: a for a in load("evaluation/results/answers-groq.json")["answers"]}
    probes = {p["id"]: p for p in load("evaluation/results/security-groq.json")["probe_details"]}

    for item in labelled:
        if item["question"] != question:
            continue
        if item["kind"] == "forbidden" and item["asker"] == person:
            probe = probes[item["id"]]
            if probe["refused"] and not probe["leaked"]:
                return NO_INFORMATION
        controlled = item["kind"] == "forbidden" and item.get("control") == person
        if controlled and probes[item["id"]]["control_passed"]:
            return f"An answer citing {item['document']}"
        if item["kind"] == "answerable" and item["asker"] == person:
            answer = answers[item["id"]]
            cited = {s["document"] for s in answer["sources"] if s["label"] in answer["cited"]}
            if answer["status"] == "answered" and item["document"] in cited:
                return f"An answer citing {item['document']}"
    return None


# --- part A, against what was measured --------------------------------------------------


def test_part_a_has_its_pairs() -> None:
    rows = part_a()

    assert len(rows) == 6
    assert [said == NO_INFORMATION for *_, said in rows] == [True, False] * 3


@pytest.mark.parametrize(("step", "person", "question", "said"), part_a(), ids=lambda v: str(v))
def test_each_row_of_part_a_is_what_the_evaluation_measured(
    step: str, person: str, question: str, said: str
) -> None:
    assert measured(person, question) == said, f"{step}: nobody measured {person} getting {said}"


def test_the_demo_pages_examples_are_part_a() -> None:
    tree = ast.parse((ROOT / "ui" / "streamlit_app.py").read_text(encoding="utf-8"))
    (examples,) = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "EXAMPLES"
    ]

    assert ast.literal_eval(examples) == [(person, question) for _, person, question, _ in part_a()]


def test_every_step_of_part_b_names_a_test_that_exists() -> None:
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("## Part B", 1)[1].split("\n## ", 1)[0]
    steps = re.findall(r"^\*\*(B\d+)\. ", section, re.MULTILINE)
    named = _SHOWN_BY.findall(section)

    assert steps == [f"B{n}" for n in range(1, len(steps) + 1)]
    assert len(named) == len(steps)
    for step, reference in zip(steps, named, strict=True):
        check_references(step, reference)
        assert f"::test_{step.lower()}_" in reference, (step, reference)


# --- part B, through the API ------------------------------------------------------------


@pytest.fixture
def client(db: Session, engine: Engine, settings: Settings) -> Any:
    with company_client(db, engine, settings) as client:
        yield client


def paper_id(client: TestClient) -> UUID:
    with client.app.state.session_factory() as db:  # type: ignore[attr-defined]
        found: UUID = db.scalars(select(Document).where(Document.title == PAPER)).one().public_id
    return found


def titles(client: TestClient, auth: dict[str, str]) -> set[str]:
    response = client.get("/documents", headers=auth)
    assert response.status_code == 200
    return {d["title"] for d in response.json()["documents"]}


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    error = dict(body["error"])
    error.pop("request_id")
    return {"error": error}


def test_b1_henry_manages_every_document_without_reading_one(client: TestClient) -> None:
    henry = headers(client, "henry")

    listed = client.get("/admin/documents", headers=henry).json()
    me = client.get("/auth/me", headers=henry).json()

    assert listed["total"] == 17
    assert not any("title" in d or "source_filename" in d for d in listed["documents"])
    assert not any(p.startswith("documents:read:") for p in me["permissions"])
    assert titles(client, henry) == set()
    # How he finds the paper: restricted, Executive, granted to Grace and Emma.
    (paper,) = [
        d
        for d in listed["documents"]
        if d["classification"] == "restricted" and d["department"] == "executive"
    ]
    assert paper["id"] == str(paper_id(client))
    assert {g["email"] for g in paper["grants"]} == {email("grace"), email("emma")}


def test_b2_a_hidden_document_is_the_same_404_as_a_missing_one(client: TestClient) -> None:
    david = headers(client, "david")

    hidden = client.get(f"/documents/{paper_id(client)}", headers=david)
    missing = client.get(f"/documents/{MISSING}", headers=david)

    assert len(titles(client, david)) == 4
    assert hidden.status_code == missing.status_code == 404
    assert without_request_id(hidden.json()) == without_request_id(missing.json())
    assert hidden.headers.keys() == missing.headers.keys()


def test_b3_a_grant_applies_at_once_and_so_does_revoking_it(client: TestClient) -> None:
    henry, david = headers(client, "henry"), headers(client, "david")
    paper = paper_id(client)

    granted = client.put(
        f"/admin/documents/{paper}/grants", headers=henry, json={"email": email("david")}
    )
    assert granted.status_code == 200
    assert PAPER in titles(client, david)
    assert client.get(f"/documents/{paper}", headers=david).status_code == 200

    revoked = client.delete(f"/admin/documents/{paper}/grants/{email('david')}", headers=henry)
    assert revoked.status_code == 204
    assert PAPER not in titles(client, david)
    assert client.get(f"/documents/{paper}", headers=david).status_code == 404


def test_b4_the_reviewer_sees_every_step_and_what_the_responses_hid(client: TestClient) -> None:
    henry, david, irene = (
        headers(client, "henry"),
        headers(client, "david"),
        headers(client, "irene"),
    )
    paper = paper_id(client)
    client.get(f"/documents/{paper}", headers=david)
    client.get(f"/documents/{MISSING}", headers=david)
    client.put(f"/admin/documents/{paper}/grants", headers=henry, json={"email": email("david")})
    client.delete(f"/admin/documents/{paper}/grants/{email('david')}", headers=henry)

    by_david = client.get("/audit", headers=irene, params={"actor": email("david")}).json()
    by_henry = client.get("/audit", headers=irene, params={"actor": email("henry")}).json()

    reads = [
        (r["outcome"], r["document_id"], r["details"].get("requested_id"))
        for r in by_david["records"]
        if r["action"] == "document.read"
    ]
    # The responses were the same; the trail tells them apart. A missing ID
    # names no document, so the ID asked for is kept beside it.
    assert ("denied", str(paper), None) in reads
    assert ("not_found", None, str(MISSING)) in reads
    assert {"grant.given", "grant.revoked"} <= {r["action"] for r in by_henry["records"]}


def test_b5_signing_out_withdraws_the_token(client: TestClient) -> None:
    david = headers(client, "david")
    assert client.get("/documents", headers=david).status_code == 200

    assert client.post("/auth/logout", headers=david).status_code == 204
    assert client.get("/documents", headers=david).status_code == 401
