"""Text from the service, made safe to draw where Streamlit reads Markdown.

Headings, captions, messages and widget labels all read Markdown, images
included. A document title, a name or an error message is data, and drawn
as Markdown a title such as ``![x](https://example.com/?q=...)`` would make
the reader's browser fetch that URL without a click (threat T29). Long text,
an answer or a document, is drawn with ``st.text`` instead and needs none of
this.
"""

import re

# CommonMark lets any ASCII punctuation be escaped with a backslash, and an
# escaped character is always shown as itself. Escaping all of it, rather
# than the characters known to matter today, leaves nothing for a later
# Markdown feature (or Streamlit's own extensions, such as :colour[...] and
# $maths$) to pick up.
_PUNCTUATION = re.compile(r"""([!"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])""")

# How much a browser is shown of an error nothing on the page caught: its
# type and Streamlit's own "the original error message is redacted". By
# default Streamlit shows the message and the traceback. A message holds
# whatever the code was working with, which on the demo page, where the
# service runs in the page's process, can be any document; the traceback is
# the source. Both still go to the console the page was started from.
ERROR_DETAILS = "type"


def literal(text: str) -> str:
    """``text`` as Markdown that shows exactly ``text``, on one line."""
    # A line break could start a heading, list or block quote, and a label
    # is one line anyway.
    flat = " ".join(text.split())
    return _PUNCTUATION.sub(r"\\\1", flat)
