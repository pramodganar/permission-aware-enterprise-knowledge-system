"""Turning an uploaded file into text.

One function per file type, looked up by extension. An uploaded file is
untrusted input: an extractor must either return text or raise
InvalidInputError, and never let a parser error escape.
"""

import io
import logging
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

    return "\n\n".join(page.strip() for page in pages if page.strip())


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
    except zipfile.BadZipFile:
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
