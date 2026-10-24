"""Turning an uploaded file into text.

One function per file type, looked up by extension. An uploaded file is
untrusted input: an extractor must either return text or raise
InvalidInputError, and never let a parser error escape.
"""

import io
import logging
import re
import zipfile
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType

from docx import Document as open_docx
from docx.table import Table
from pypdf import PdfReader

from app.core.exceptions import InvalidInputError

Extractor = Callable[[bytes], str]

logger = logging.getLogger(__name__)

NOT_TEXT = "the file is not a readable text file"
_UTF16_MARKS = (b"\xff\xfe", b"\xfe\xff")
# Every control character except tab, line feed, form feed and carriage return.
_CONTROL_CHARACTERS = frozenset(map(chr, range(32))) - frozenset("\t\n\f\r")

MAX_PDF_PAGES = 500

# A page header or footer is looked for in this many lines at each end of a page.
PAGE_EDGE_LINES = 2
_DIGITS = re.compile(r"\d+")

# A .docx file is a zip archive. These limits stop a small upload from
# unpacking into something enormous.
MAX_DOCX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_DOCX_ENTRIES = 2000


def extract_txt(data: bytes) -> str:
    """Decode a text file.

    UTF-8 is tried first. UTF-16 is accepted when the file says so with a byte
    order mark. Anything else is read as Windows-1252, which is what older
    Windows tools write. A file in some other legacy encoding will decode
    without error but with wrong characters, so the fallback is logged.
    """
    text = _decode(data)
    if any(character in text for character in _CONTROL_CHARACTERS):
        # Real text doesn't contain these. A binary file almost always does.
        raise InvalidInputError(NOT_TEXT)
    return text


def _decode(data: bytes) -> str:
    if data.startswith(_UTF16_MARKS):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise InvalidInputError(NOT_TEXT) from None

    try:
        # utf-8-sig also accepts plain UTF-8 and drops a leading byte order mark.
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    try:
        text = data.decode("cp1252")
    except UnicodeDecodeError:
        raise InvalidInputError(NOT_TEXT) from None
    logger.warning("text file was not UTF-8, read as Windows-1252", extra={"bytes": len(data)})
    return text


def extract_pdf(data: bytes) -> str:
    """Return the text layer of a PDF, page by page.

    A scanned PDF has no text layer and comes back empty. There is no OCR.
    Lines that repeat at the top or bottom of every page are left out.
    """
    if not data.startswith(b"%PDF-"):
        raise InvalidInputError("the file is not a PDF")

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise InvalidInputError("the PDF is password protected")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise InvalidInputError(f"the PDF has more than {MAX_PDF_PAGES} pages")
        pages = [page.extract_text() or "" for page in reader.pages]
    except InvalidInputError:
        raise
    except Exception:
        # Whatever the parser tripped over, the caller only needs to know the
        # file was not usable.
        raise InvalidInputError("the PDF could not be read") from None

    pages = _without_page_furniture([page.strip() for page in pages if page.strip()])
    return "\n\n".join(pages)


def _without_page_furniture(pages: list[str]) -> list[str]:
    """Drop headers and footers: lines that repeat at the same edge of every page.

    A line counts as repeated when it is the same on every page once digits
    are ignored, so "Page 1 of 3" and "Page 2 of 3" match. Left in, such lines
    end up in the middle of sentences that run over a page break, and a
    marking like "Confidential" becomes text a search can match.

    Only the outer lines of a page are looked at, and nothing is removed from
    a document of one page. If every line would go, the text is left alone.
    """
    if len(pages) < 2:
        return pages

    lines = [[line for line in page.split("\n") if line.strip()] for page in pages]
    repeated = [
        position
        for position in (*range(PAGE_EDGE_LINES), *range(-PAGE_EDGE_LINES, 0))
        if len({_furniture_key(page, position) for page in lines}) == 1
        and _furniture_key(lines[0], position) is not None
    ]
    if not repeated:
        return pages

    kept = [
        "\n".join(
            line
            for index, line in enumerate(page)
            if index not in repeated and index - len(page) not in repeated
        )
        for page in lines
    ]
    if not any(kept):
        return pages

    logger.info(
        "removed repeated page lines from PDF text",
        extra={"pages": len(pages), "lines_per_page": len(repeated)},
    )
    return [page for page in kept if page]


def _furniture_key(page_lines: list[str], position: int) -> str | None:
    """The line at a position with digits masked, or None if the page is too short."""
    if not -len(page_lines) <= position < len(page_lines):
        return None
    return _DIGITS.sub("#", " ".join(page_lines[position].split()))


def extract_docx(data: bytes) -> str:
    """Return the paragraphs and tables of a Word document, in document order.

    Headers, footers, footnotes and text boxes are not included.
    """
    _check_docx_archive(data)

    try:
        document = open_docx(io.BytesIO(data))
        blocks = [
            _table_text(block) if isinstance(block, Table) else block.text
            for block in document.iter_inner_content()
        ]
    except Exception:
        raise InvalidInputError("the Word document could not be read") from None

    return "\n".join(block for block in blocks if block.strip())


def _check_docx_archive(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except Exception:
        # Not only BadZipFile: a damaged archive can also give
        # NotImplementedError ("zip file version 21.5"), found by fuzzing, and
        # it was a 500 until then.
        raise InvalidInputError("the file is not a Word document") from None

    if len(entries) > MAX_DOCX_ENTRIES:
        raise InvalidInputError("the Word document has too many parts")
    if sum(entry.file_size for entry in entries) > MAX_DOCX_UNPACKED_BYTES:
        raise InvalidInputError("the Word document is too large when unpacked")
    if "word/document.xml" not in {entry.filename for entry in entries}:
        raise InvalidInputError("the file is not a Word document")


def _table_text(table: Table) -> str:
    return "\n".join(" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows)


EXTRACTORS: Mapping[str, Extractor] = MappingProxyType(
    {
        ".txt": extract_txt,
        ".pdf": extract_pdf,
        ".docx": extract_docx,
    }
)


def base_name(filename: str) -> str:
    """Return the file name without any directory part, whichever slashes were used."""
    return PurePosixPath(PureWindowsPath(filename).name).name


def extension_of(filename: str) -> str:
    return PurePosixPath(base_name(filename)).suffix.lower()


def extract_text(filename: str, data: bytes) -> str:
    """Extract text using the extractor for this file's extension."""
    extractor = EXTRACTORS.get(extension_of(filename))
    if extractor is None:
        supported = ", ".join(sorted(EXTRACTORS))
        raise InvalidInputError(f"unsupported file type; supported types: {supported}")
    return extractor(data)
