"""The web UI (FR-16). Run with: streamlit run ui/app.py

It talks only to the HTTP API, at UI_API_URL (default http://127.0.0.1:8000),
so the service has to be running:

    uvicorn app.main:serve --factory --no-access-log
    streamlit run ui/app.py

Everything shown here came from the API for the signed-in person's token.
Which parts of the page are offered follows the permissions the service
reports, but that is only so people aren't offered what they'd be refused:
the API refuses it either way.
"""

import os
import sys
from pathlib import Path

import streamlit as st

# Started as a script from anywhere, the ui package lives one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui import asking
from ui.client import (
    DEFAULT_API_URL,
    Answer,
    Api,
    ApiError,
    Citation,
    Filters,
    Me,
    NotSignedIn,
    Passage,
    SearchResult,
)
from ui.session import (
    ASK,
    NOTICE,
    SESSION_ENDED,
    current_user,
    pages_for,
    sign_in,
    sign_out,
    token,
    what_they_can_do,
)

TITLE = "AcmeCorp Knowledge Assistant"


@st.cache_resource
def api(base_url: str) -> Api:
    # One client, with its connection pool, shared by every visitor's session.
    return Api(base_url)


EMAIL_FIELD = "sign_in_email"
PASSWORD_FIELD = "sign_in_password"  # noqa: S105 - the field's name, not a password
PROBLEM = "sign_in_problem"


def submit_sign_in(client: Api) -> None:
    """Runs before the page is drawn again, so the password can be dropped at once.

    A widget keeps its value in the visitor's session on the server, and
    clearing the form in the browser doesn't change that. Blanking the field
    here does: the password exists only for this call.
    """
    email = str(st.session_state.get(EMAIL_FIELD, ""))
    password = str(st.session_state.get(PASSWORD_FIELD, ""))
    st.session_state[PASSWORD_FIELD] = ""
    st.session_state[PROBLEM] = sign_in(client, st.session_state, email, password)


def show_sign_in(client: Api) -> None:
    st.title(TITLE)
    notice = st.session_state.get(NOTICE)
    if notice:
        st.info(notice)
    with st.form("sign_in"):
        st.text_input("Email", key=EMAIL_FIELD)
        st.text_input("Password", type="password", key=PASSWORD_FIELD)
        st.form_submit_button("Sign in", on_click=submit_sign_in, args=(client,))
    problem = st.session_state.pop(PROBLEM, None)
    if problem:
        st.error(problem)
    st.caption("AcmeCorp is a fictional company. Every person and document here is made up.")


def show_sidebar(me: Me) -> str:
    """Draw the sidebar and return the page chosen."""
    with st.sidebar:
        st.subheader(me.full_name)
        st.caption(f"{me.department.name} · {', '.join(me.roles) or 'no role'}")
        # Offered afresh on every interaction from what the service said, so a
        # page whose permission was taken away disappears on the next click.
        page = str(st.radio("Page", pages_for(me), key="page"))
        if st.button("Sign out"):
            sign_out(st.session_state)
            st.rerun()
    return page


def show_home(me: Me) -> None:
    st.title(TITLE)
    notice = st.session_state.get(NOTICE)
    if notice:
        st.warning(notice)
    st.write(f"Signed in as **{me.email}**.")
    st.markdown("\n".join(f"- {line}" for line in what_they_can_do(me)))


def show_passage(heading: str, text: str) -> None:
    with st.expander(heading):
        # Plain text, never Markdown: a document could hold an image link
        # that the browser would fetch, carrying whatever is in its URL.
        st.text(text)


def show_citation(citation: Citation) -> None:
    document = citation.document
    show_passage(f"[{citation.label}] {document.title}, version {document.version}", citation.text)


def show_answer(answer: Answer) -> None:
    if not answer.answered:
        st.info(answer.answer)
        return
    # The model's words are shown as plain text for the same reason as the
    # passages: they are made from the documents and could carry a link.
    st.text(answer.answer)
    if answer.cut_off:
        st.warning(asking.CUT_OFF)
    if answer.uncited:
        st.warning(asking.UNCITED)
    if answer.citations:
        st.markdown("**Sources**")
        for citation in answer.citations:
            show_citation(citation)


def show_results(result: SearchResult) -> None:
    if not result.results:
        st.info(result.message or "No results.")
        return
    st.markdown(f"**{len(result.results)} passages you may read, best first**")
    for passage in result.results:
        show_search_result(passage)


def show_search_result(passage: Passage) -> None:
    document = passage.document
    show_passage(
        f"{document.title}, version {document.version} · relevance {passage.score:.2f}",
        passage.text,
    )


def ask_form(choices: asking.Choices) -> tuple[str, Filters, str | None]:
    """Draw the question form. Returns the question, filters and the button pressed."""
    with st.form("question"):
        question = st.text_area("Question", max_chars=asking.MAX_QUESTION, key="question")
        with st.expander(asking.NARROW):
            departments = st.multiselect(
                "Departments",
                list(choices.departments),
                format_func=lambda code: choices.departments.get(code, code),
            )
            classifications = st.multiselect("Classifications", list(choices.classifications))
            documents = st.multiselect(
                "Documents",
                list(choices.documents),
                format_func=lambda key: choices.documents.get(key, str(key)),
            )
        left, right = st.columns(2)
        ask = left.form_submit_button("Ask", type="primary")
        search = right.form_submit_button("Search only")
    pressed = "ask" if ask else "search" if search else None
    return question, asking.chosen(departments, classifications, documents), pressed


def show_ask(client: Api) -> None:
    st.title(ASK)
    st.caption(
        "Answers come only from documents you may read, and say which ones. "
        "Search shows the passages without asking the model."
    )
    try:
        choices = asking.choices(asking.readable_documents(client, token(st.session_state)))
        if choices.empty:
            st.info("There are no documents you may read yet.")
            return
        question, filters, pressed = ask_form(choices)
        if pressed is None:
            return
        problem = asking.problem_with(question, filters)
        if problem:
            st.error(problem)
            return
        if pressed == "ask":
            with st.spinner("Answering from the documents you may read…"):
                answer = client.ask(token(st.session_state), question.strip(), filters)
            show_answer(answer)
        else:
            show_results(client.search(token(st.session_state), question.strip(), filters))
    except NotSignedIn:
        sign_out(st.session_state, notice=SESSION_ENDED)
        st.rerun()
    except ApiError as error:
        st.error(asking.explain(error))


def main() -> None:
    st.set_page_config(page_title=TITLE, layout="wide")
    client = api(os.environ.get("UI_API_URL", DEFAULT_API_URL))
    me = current_user(client, st.session_state)
    if me is None:
        show_sign_in(client)
        return
    page = show_sidebar(me)
    if page == ASK:
        show_ask(client)
    else:
        show_home(me)


main()
