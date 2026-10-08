"""The access matrix from docs/personas.md, checked for the whole company.

Every persona is checked against every document, in both forms the
authorization service offers. The expected answers are written out here and
do not come from the code under test.
"""

import pytest

from acmecorp import COMPANY_DOCUMENTS, DEPARTMENTS, PERSONAS, World
from app.models import Classification

NON_READERS = ("admin", "reviewer", "nobody")

GENERAL = [name for name, (_, _, level) in COMPANY_DOCUMENTS.items() if level == "general"]
DEPARTMENTAL = [
    name for name, (_, _, level) in COMPANY_DOCUMENTS.items() if level == "departmental"
]
RESTRICTED = [name for name, (_, _, level) in COMPANY_DOCUMENTS.items() if level == "restricted"]

# The table in docs/personas.md: which departments' documents each persona reads.
READS_DEPARTMENTS = {
    "alice": {"engineering"},
    "bob": {"finance"},
    "carol": {"hr"},
    "david": {"sales"},
    "emma": {"legal"},
    "frank": {"operations"},
    "grace": set(DEPARTMENTS),
}


def owner(document: str) -> str:
    return COMPANY_DOCUMENTS[document][1]


def expected(persona: str, document: str) -> bool:
    level = COMPANY_DOCUMENTS[document][2]
    if level == Classification.GENERAL:
        return True
    if level == Classification.DEPARTMENTAL:
        return owner(document) in READS_DEPARTMENTS[persona]
    return False


def test_the_test_data_matches_the_personas_document() -> None:
    assert set(READS_DEPARTMENTS) == set(PERSONAS)
    assert {owner(document) for document in DEPARTMENTAL} == set(DEPARTMENTS)
    assert len(GENERAL) == 3
    assert len(DEPARTMENTAL) == 12
    assert len(RESTRICTED) == 2


# The matrix


@pytest.mark.parametrize("document", list(COMPANY_DOCUMENTS))
@pytest.mark.parametrize("persona", list(PERSONAS))
def test_persona_can_read_exactly_what_the_matrix_says(
    company: World, persona: str, document: str
) -> None:
    assert company.can_read(persona, document) is expected(persona, document)


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_scope_query_matches_the_matrix(company: World, persona: str) -> None:
    should_read = {document for document in COMPANY_DOCUMENTS if expected(persona, document)}

    assert company.readable(persona) == should_read


def test_the_matrix_has_the_expected_number_of_allowed_and_refused_pairs(company: World) -> None:
    allowed = sum(
        company.can_read(persona, document)
        for persona in PERSONAS
        for document in COMPANY_DOCUMENTS
    )
    total = len(PERSONAS) * len(COMPANY_DOCUMENTS)

    # General: 7 personas x 3 documents. Own department: the six employees read
    # 2+2+2+1+2+1 = 10. Grace reads all 12 departmental documents.
    assert total == 119
    assert allowed == 21 + 10 + 12
    assert total - allowed == 76


# Each rule, stated across the whole company


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_everyone_reads_every_general_document(company: World, persona: str) -> None:
    assert all(company.can_read(persona, document) for document in GENERAL)


@pytest.mark.parametrize("persona", [name for name in PERSONAS if name != "grace"])
def test_employee_reads_own_department_and_no_other(company: World, persona: str) -> None:
    home = PERSONAS[persona]

    for document in DEPARTMENTAL:
        assert company.can_read(persona, document) is (owner(document) == home), document


def test_no_employee_can_read_across_any_pair_of_departments(company: World) -> None:
    crossings = [
        (persona, document)
        for persona, home in PERSONAS.items()
        if persona != "grace"
        for document in DEPARTMENTAL
        if owner(document) != home and company.can_read(persona, document)
    ]

    assert crossings == []


def test_executive_reads_every_departmental_document(company: World) -> None:
    assert all(company.can_read("grace", document) for document in DEPARTMENTAL)


@pytest.mark.parametrize("document", RESTRICTED)
def test_restricted_documents_are_read_by_nobody_without_a_grant(
    company: World, document: str
) -> None:
    assert not any(company.can_read(user, document) for user in company.users)


@pytest.mark.parametrize("user", NON_READERS)
def test_accounts_without_a_reading_role_read_nothing(company: World, user: str) -> None:
    assert company.readable(user) == set()
    assert not any(company.can_read(user, document) for document in COMPANY_DOCUMENTS)


# Control: a refusal must come from the rules, not from a document being unreadable by anyone


@pytest.mark.parametrize("document", DEPARTMENTAL)
def test_every_departmental_document_has_readers(company: World, document: str) -> None:
    readers = {persona for persona in PERSONAS if company.can_read(persona, document)}
    home_persona = next(name for name, home in PERSONAS.items() if home == owner(document))

    assert home_persona in readers
    assert "grace" in readers


# The questions in docs/personas.md, mapped to the document that answers each

SHOULD_BE_ANSWERED = [
    ("alice", "runbook"),
    ("alice", "leave_policy"),
    ("bob", "payment_limits"),
    ("carol", "pip_process"),
    ("david", "discount_policy"),
    ("emma", "retention"),
    ("frank", "warehouse_sop"),
    ("grace", "exec_comp"),
]

MUST_BE_REFUSED = [
    ("alice", "exec_comp"),
    ("alice", "salary_bands"),
    ("bob", "postmortem"),
    ("bob", "strategy"),
    ("carol", "headcount_budget"),
    ("carol", "runbook"),
    ("david", "supplier_dispute"),
    ("david", "exec_comp"),
    ("emma", "discount_policy"),
    ("emma", "pip_process"),
    ("frank", "payment_limits"),
    ("frank", "strategy"),
]


@pytest.mark.parametrize(("persona", "document"), SHOULD_BE_ANSWERED)
def test_persona_question_that_should_be_answered(
    company: World, persona: str, document: str
) -> None:
    assert company.can_read(persona, document)
    assert document in company.readable(persona)


@pytest.mark.parametrize(("persona", "document"), MUST_BE_REFUSED)
def test_persona_question_that_must_be_refused(company: World, persona: str, document: str) -> None:
    assert not company.can_read(persona, document)
    assert document not in company.readable(persona)


@pytest.mark.parametrize(("persona", "document"), MUST_BE_REFUSED)
def test_every_refused_question_is_answerable_by_the_executive(
    company: World, persona: str, document: str
) -> None:
    assert company.can_read("grace", document)


def test_salary_bands_follow_the_owning_department_not_the_topic(company: World) -> None:
    # The document is about engineers' pay, but HR owns it.
    assert not company.can_read("alice", "salary_bands")
    assert company.can_read("carol", "salary_bands")


# Grants across the full company


def test_grant_opens_one_restricted_document_to_one_person(company: World) -> None:
    company.grant("case_file", "carol")

    readers = {user for user in company.users if company.can_read(user, "case_file")}

    assert readers == {"carol"}
    assert not any(company.can_read(user, "acquisition") for user in company.users)


def test_cross_department_grant_does_not_spread(company: World) -> None:
    company.grant("runbook", "bob")

    assert company.readable("bob") == {*GENERAL, "payment_limits", "headcount_budget", "runbook"}
    assert not company.can_read("bob", "postmortem")
