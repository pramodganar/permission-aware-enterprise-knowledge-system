import logging

import pytest

from acmecorp import World
from app.core.config import Settings
from app.core.exceptions import InvalidInputError
from app.ingestion import extractors
from app.ingestion.extractors import extract_docx, extract_pdf, extract_text, extract_txt
from app.ingestion.pipeline import Upload, ingest
from files import make_docx, make_encrypted_pdf, make_pdf, make_zip

EXE_HEADER = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff"


def upload(filename: str, data: bytes) -> Upload:
    return Upload(
        filename=filename,
        data=data,
        title="Test Document",
        department_code="engineering",
        classification="departmental",
    )


# Text


@pytest.mark.parametrize(
    ("data", "text"),
    [
        ("Zürich café".encode(), "Zürich café"),
        (b"\xef\xbb\xbfHello", "Hello"),
        ("Zürich café".encode("utf-16"), "Zürich café"),
        (b"\xfe\xff" + "Zürich café".encode("utf-16-be"), "Zürich café"),
        ("Zürich café".encode("cp1252"), "Zürich café"),
        (
            # Curly quotes and a dash on purpose: Windows-1252 stores them differently.
            "Price: 50€ – “quoted”".encode("cp1252"),
            "Price: 50€ – “quoted”",
        ),
    ],
    ids=["utf-8", "utf-8 mark", "utf-16 le", "utf-16 be", "cp1252", "cp1252 punctuation"],
)
def test_text_file_encodings(data: bytes, text: str) -> None:
    assert extract_txt(data) == text


def test_tabs_and_line_breaks_are_ordinary_text() -> None:
    text = "Role\tLimit\r\nAnalyst\t5,000\n\x0cNext page"

    assert extract_txt(text.encode()) == text


@pytest.mark.parametrize(
    "data",
    [
        b"abc\x00def",
        b"\x01\x02\x03",
        b"bell \x07",
        b"escape \x1b[31m",
        EXE_HEADER,
        b"\x81\x8d\x8f",
        b"\xff\xfe\x00",
        "zero\x00inside".encode("utf-16"),
    ],
    ids=[
        "zero byte",
        "control bytes",
        "bell",
        "escape code",
        "executable",
        "undefined in cp1252",
        "odd-length utf-16",
        "utf-16 with zero",
    ],
)
def test_binary_content_is_not_accepted_as_text(data: bytes) -> None:
    with pytest.raises(InvalidInputError, match="not a readable text file"):
        extract_txt(data)


def test_fallback_encoding_is_logged_but_utf8_is_not(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.ingestion.extractors")

    extract_txt("café".encode())
    assert caplog.records == []

    extract_txt("café".encode("cp1252"))
    assert caplog.records[-1].getMessage() == "text file was not UTF-8, read as Windows-1252"
    assert "caf" not in str(vars(caplog.records[-1]))


# PDF


def test_pdf_text_is_extracted() -> None:
    assert extract_pdf(make_pdf(["Rollbacks are approved by on-call."])) == (
        "Rollbacks are approved by on-call."
    )


def test_pdf_pages_are_joined_in_order() -> None:
    text = extract_pdf(make_pdf(["First page.", "Second page.", "Third page."]))

    assert text == "First page.\n\nSecond page.\n\nThird page."


def test_blank_pdf_pages_are_skipped() -> None:
    assert extract_pdf(make_pdf(["First page.", "", "Third page."])) == "First page.\n\nThird page."


def test_pdf_with_no_text_layer_gives_empty_text() -> None:
    assert extract_pdf(make_pdf(["", ""])) == ""


@pytest.mark.parametrize(
    "data",
    [b"This is just text.", EXE_HEADER, b"", b"PK\x03\x04", b"  %PDF-1.4"],
)
def test_file_that_does_not_start_like_a_pdf_is_rejected(data: bytes) -> None:
    with pytest.raises(InvalidInputError, match="not a PDF"):
        extract_pdf(data)


@pytest.mark.parametrize(
    "data",
    [
        b"%PDF-1.4\n",
        b"%PDF-1.4\nthis is not really a pdf at all\n%%EOF",
        b"%PDF-1.4\n" + b"\x00" * 2000,
        make_pdf(["Cut off."])[:120],
    ],
    ids=["header only", "not really a pdf", "zero bytes", "truncated"],
)
def test_broken_pdf_is_rejected_cleanly(data: bytes) -> None:
    with pytest.raises(InvalidInputError, match="could not be read"):
        extract_pdf(data)


def test_password_protected_pdf_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="password protected"):
        extract_pdf(make_encrypted_pdf())


def test_pdf_with_too_many_pages_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractors, "MAX_PDF_PAGES", 3)

    assert extract_pdf(make_pdf(["a", "b", "c"]))
    with pytest.raises(InvalidInputError, match="more than 3 pages"):
        extract_pdf(make_pdf(["a", "b", "c", "d"]))


# Word


def test_docx_paragraphs_are_extracted() -> None:
    data = make_docx(["Leave Policy", "Employees get 25 days of annual leave."])

    assert extract_docx(data) == "Leave Policy\nEmployees get 25 days of annual leave."


def test_docx_tables_are_extracted_in_document_order() -> None:
    data = make_docx(
        ["Approval limits:"],
        table=[["Role", "Limit"], ["Analyst", "5,000"], ["Manager", "50,000"]],
        after_table=["Above 50,000 needs the CFO."],
    )

    assert extract_docx(data) == (
        "Approval limits:\n"
        "Role | Limit\n"
        "Analyst | 5,000\n"
        "Manager | 50,000\n"
        "Above 50,000 needs the CFO."
    )


def test_empty_docx_paragraphs_are_skipped() -> None:
    assert extract_docx(make_docx(["One", "", "   ", "Two"])) == "One\nTwo"


def test_docx_with_no_text_gives_empty_text() -> None:
    assert extract_docx(make_docx([])) == ""


def test_docx_keeps_non_latin_text() -> None:
    assert extract_docx(make_docx(["नीति दस्तावेज़ — Zürich 東京"])) == "नीति दस्तावेज़ — Zürich 東京"


@pytest.mark.parametrize("data", [b"This is just text.", EXE_HEADER, b"", b"%PDF-1.4\n"])
def test_file_that_is_not_a_zip_is_rejected_as_docx(data: bytes) -> None:
    with pytest.raises(InvalidInputError, match="not a Word document"):
        extract_docx(data)


def test_ordinary_zip_renamed_to_docx_is_rejected() -> None:
    data = make_zip({"notes.txt": b"hello", "more/data.csv": b"a,b"})

    with pytest.raises(InvalidInputError, match="not a Word document"):
        extract_docx(data)


def test_zip_with_a_broken_document_part_is_rejected_cleanly() -> None:
    data = make_zip({"word/document.xml": b"<not-closed", "[Content_Types].xml": b"<x/>"})

    with pytest.raises(InvalidInputError, match="could not be read"):
        extract_docx(data)


def test_truncated_docx_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="not a Word document"):
        extract_docx(make_docx(["Cut off."])[:300])


def test_docx_that_unpacks_too_large_is_rejected_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extractors, "MAX_DOCX_UNPACKED_BYTES", 1024 * 1024)
    bomb = make_zip({"word/document.xml": b"\x00" * (3 * 1024 * 1024)})

    def fail_if_called(*args: object) -> None:
        raise AssertionError("an oversized archive must not be handed to the parser")

    monkeypatch.setattr(extractors, "open_docx", fail_if_called)

    # The upload itself is tiny; only its unpacked size gives it away.
    assert len(bomb) < 10 * 1024
    with pytest.raises(InvalidInputError, match="too large when unpacked"):
        extract_docx(bomb)


def test_docx_with_too_many_parts_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractors, "MAX_DOCX_ENTRIES", 5)
    data = make_zip({f"part{number}.xml": b"<x/>" for number in range(6)})

    with pytest.raises(InvalidInputError, match="too many parts"):
        extract_docx(data)


# Choosing the extractor


def test_supported_types() -> None:
    assert sorted(extractors.EXTRACTORS) == [".docx", ".pdf", ".txt"]


@pytest.mark.parametrize("filename", ["memo.doc", "sheet.xlsx", "deck.pptx", "page.html", "a.rtf"])
def test_other_office_formats_are_not_supported(filename: str) -> None:
    with pytest.raises(InvalidInputError, match=r"supported types: \.docx, \.pdf, \.txt"):
        extract_text(filename, b"data")


def test_extension_decides_the_extractor_and_content_must_match() -> None:
    pdf, docx = make_pdf(["Hello"]), make_docx(["Hello"])

    assert extract_text("a.PDF", pdf) == "Hello"
    assert extract_text("a.Docx", docx) == "Hello"
    with pytest.raises(InvalidInputError, match="not a PDF"):
        extract_text("renamed.pdf", docx)
    with pytest.raises(InvalidInputError, match="not a Word document"):
        extract_text("renamed.docx", pdf)
    with pytest.raises(InvalidInputError, match="not a readable text file"):
        extract_text("renamed.txt", docx)
    with pytest.raises(InvalidInputError, match="not a readable text file"):
        extract_text("renamed.txt", pdf + b"\x00")


@pytest.mark.parametrize("filename", ["program.pdf", "program.docx", "program.txt"])
def test_executable_renamed_to_a_supported_type_is_rejected(filename: str) -> None:
    with pytest.raises(InvalidInputError):
        extract_text(filename, EXE_HEADER + b"\x00" * 200)


# Through the pipeline


def test_pdf_is_ingested_end_to_end(world: World, settings: Settings) -> None:
    data = make_pdf(["Rollbacks are approved by on-call.", "Escalate after 30 minutes."])

    document = ingest(world.db, world.users["admin"], upload("runbook.pdf", data), settings)

    assert document.content == "Rollbacks are approved by on-call.\n\nEscalate after 30 minutes."


def test_docx_is_ingested_end_to_end(world: World, settings: Settings) -> None:
    data = make_docx(["Vendor payments over 5,000 need a manager."])

    document = ingest(world.db, world.users["admin"], upload("limits.docx", data), settings)

    assert document.content == "Vendor payments over 5,000 need a manager."


@pytest.mark.parametrize(
    ("filename", "data"),
    [("scan.pdf", make_pdf(["", ""])), ("blank.docx", make_docx([]))],
    ids=["scanned pdf", "blank docx"],
)
def test_document_with_no_extractable_text_is_not_stored(
    world: World, settings: Settings, filename: str, data: bytes
) -> None:
    with pytest.raises(InvalidInputError, match="no text could be extracted"):
        ingest(world.db, world.users["admin"], upload(filename, data), settings)


# --- page headers and footers in PDF text ---------------------------------------------


def page(number: int, body: str, total: int = 3) -> str:
    return "\n".join(
        [
            "ACMECORP Finance",
            body,
            f"Confidential - Finance Page {number} of {total}",
            "Uncontrolled when printed.",
        ]
    )


def test_pdf_lines_repeated_on_every_page_are_left_out() -> None:
    data = make_pdf(
        [page(1, "Payments need approval."), page(2, "Limits apply."), page(3, "Ask Finance.")]
    )

    assert extract_pdf(data) == "Payments need approval.\n\nLimits apply.\n\nAsk Finance."


def test_pdf_sentence_across_a_page_break_is_no_longer_interrupted() -> None:
    data = make_pdf(
        [page(1, "The operator does not drive again until", 2), page(2, "it is recorded.", 2)]
    )

    assert extract_pdf(data) == "The operator does not drive again until\n\nit is recorded."


def test_pdf_page_numbers_match_whatever_the_digits() -> None:
    bodies = {1: "Payments need approval.", 2: "Limits apply to invoices.", 11: "Ask Finance."}
    pages = [f"{body}\nPage {number}" for number, body in bodies.items()]

    text = extract_pdf(make_pdf(pages))

    assert text == "Payments need approval.\n\nLimits apply to invoices.\n\nAsk Finance."


def test_pdf_lines_that_differ_only_in_digits_count_as_repeated() -> None:
    # The cost of ignoring digits: a real line at the edge of every page that
    # changes only in its numbers is taken for a header.
    years = ((2025, "before"), (2026, "after"))
    pages = [
        f"Total for {year}: {year - 2000} items\nNotes on the year {word}." for year, word in years
    ]

    assert extract_pdf(make_pdf(pages)) == "Notes on the year before.\n\nNotes on the year after."


def test_pdf_with_one_page_keeps_every_line() -> None:
    assert extract_pdf(make_pdf([page(1, "Only page.", 1)])) == page(1, "Only page.", 1)


def test_pdf_line_missing_from_one_page_is_kept_everywhere() -> None:
    # A marking on the first page only is content, not a header.
    pages = ["DRAFT\nFirst page text.\nMore text.", "Second page text.\nMore words.\nAnd more."]

    assert "DRAFT" in extract_pdf(make_pdf(pages))


def test_pdf_repeated_line_in_the_middle_of_a_page_is_kept() -> None:
    body = "First line {n}.\nSecond line {n}.\nSee the policy.\nFourth line {n}.\nFifth line {n}."
    pages = [body.replace("{n}", word) for word in ("one", "two", "three")]

    assert extract_pdf(make_pdf(pages)).count("See the policy.") == 3


def test_pdf_whose_pages_are_all_the_same_is_left_alone() -> None:
    # Removing every repeated line would leave nothing, so nothing is removed.
    assert extract_pdf(make_pdf(["Notice", "Notice"])) == "Notice\n\nNotice"


def test_pdf_only_the_outer_two_lines_of_a_page_are_considered() -> None:
    pages = [
        f"Header\nSubheader\nThird line\nBody {word}.\nMore {word}.\nEnd {word}."
        for word in ("a", "b")
    ]

    text = extract_pdf(make_pdf(pages))

    assert "Header" not in text
    assert "Subheader" not in text
    assert text.count("Third line") == 2


def test_pdf_removal_is_logged_without_the_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        extract_pdf(make_pdf([page(1, "Secret limit one.", 2), page(2, "Secret limit two.", 2)]))

    record = next(r for r in caplog.records if "repeated page lines" in r.message)
    assert (record.pages, record.lines_per_page) == (2, 3)  # type: ignore[attr-defined]
    assert "Secret" not in caplog.text
    assert "Confidential" not in caplog.text
