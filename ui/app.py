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

from ui import asking, documents, managing, reviewing
from ui.client import (
    DEFAULT_API_URL,
    Answer,
    Api,
    ApiError,
    Citation,
    DocumentSummary,
    Filters,
    ManagedDocument,
    Me,
    NotFound,
    NotSignedIn,
    Passage,
    SearchResult,
)
from ui.session import (
    ASK,
    AUDIT,
    DOCUMENTS,
    MANAGE,
    NOTICE,
    SESSION_ENDED,
    current_user,
    pages_for,
    sign_in,
    sign_out,
    token,
    what_they_can_do,
)
from ui.text import ERROR_DETAILS, literal

TITLE = "AcmeCorp Knowledge Assistant"


@st.cache_resource(on_release=Api.close)
def api(base_url: str) -> Api:
    # One client, with its connection pool, shared by every visitor's session,
    # and closed when the cache lets it go.
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
        # What the service sends is drawn escaped wherever Streamlit reads
        # Markdown; see ui/text.py.
        st.subheader(literal(me.full_name))
        st.caption(literal(f"{me.department.name} · {', '.join(me.roles) or 'no role'}"))
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
    st.markdown(f"Signed in as **{literal(me.email)}**.")
    st.markdown("\n".join(f"- {line}" for line in what_they_can_do(me)))


def show_passage(heading: str, text: str) -> None:
    with st.expander(literal(heading)):
        # Plain text, never Markdown: a document could hold an image link
        # that the browser would fetch, carrying whatever is in its URL.
        st.text(text)


def show_citation(citation: Citation) -> None:
    document = citation.document
    show_passage(f"[{citation.label}] {document.title}, version {document.version}", citation.text)


def show_answer(answer: Answer) -> None:
    if not answer.answered:
        st.info(literal(answer.answer))
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
        st.info(literal(result.message or "No results."))
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
        st.error(literal(asking.explain(error)))


def show_documents(client: Api) -> None:
    st.title(DOCUMENTS)
    st.caption("The documents you may read. Opening one is recorded in the audit trail.")
    signed_in = token(st.session_state)
    try:
        offered = asking.choices(asking.readable_documents(client, signed_in))
        if offered.empty:
            st.info("There are no documents you may read yet.")
            return
        left, right = st.columns(2)
        department = left.selectbox(
            "Department",
            [None, *offered.departments],
            format_func=lambda code: "All" if code is None else offered.departments.get(code, code),
            key="documents_department",
        )
        classification = right.selectbox(
            "Classification",
            [None, *offered.classifications],
            format_func=lambda value: "All" if value is None else value,
            key="documents_classification",
        )
        page = documents.listing(
            client,
            signed_in,
            department=department,
            classification=classification,
            page=int(st.session_state.get("documents_page", 1)),
        )
        show_listing(page)
        chosen = st.selectbox(
            "Open a document",
            [None, *(d.id for d in page.documents)],
            format_func=lambda key: "" if key is None else title_of(page.documents, key),
            key="documents_open",
        )
        if chosen is not None:
            show_document(client, signed_in, int(chosen))
    except NotSignedIn:
        sign_out(st.session_state, notice=SESSION_ENDED)
        st.rerun()
    except ApiError as error:
        st.error(literal(asking.explain(error)))


def title_of(listed: list[DocumentSummary], document_id: int) -> str:
    return next((d.title for d in listed if d.id == document_id), str(document_id))


def show_listing(page: documents.Listing) -> None:
    if not page.documents:
        st.info("No documents you may read match these filters.")
        return
    shown = f"{len(page.documents)} of {page.total}" if page.pages > 1 else str(page.total)
    st.caption(f"{shown} documents")
    # A table draws its cells as plain text, so titles need no escaping here.
    st.dataframe(documents.rows(page.documents), hide_index=True, width="stretch")
    if page.pages > 1:
        st.selectbox(
            "Page",
            list(range(1, page.pages + 1)),
            index=page.page - 1,
            key="documents_page",
        )


def show_document(client: Api, signed_in: str, document_id: int) -> None:
    try:
        # Fetched again on every interaction, never kept, so a document the
        # person may no longer read is gone on their next click.
        document = client.document(signed_in, document_id)
    except NotFound:
        st.warning(documents.UNAVAILABLE)
        return
    st.subheader(literal(document.title))
    st.caption(literal(documents.about(document)))
    # The whole text as plain text: no Markdown, so nothing in it is fetched.
    st.text(document.content)


# --- managing documents -----------------------------------------------------------------

MANAGE_ID = "manage_id"
# Set by an action, applied before the ID box is drawn on the next run: a
# widget's value can't be changed once it is drawn.
MANAGE_NEXT = "manage_next"
MANAGE_DONE = "manage_done"
# Counts finished actions. The forms are named with it, so each action gets
# fresh, empty forms: a form clears itself only when its run ends, and
# st.rerun() cuts the run short, which would leave the file ready to be sent
# again.
MANAGE_ROUND = "manage_round"


def form_name(name: str) -> str:
    return f"{name} {st.session_state.get(MANAGE_ROUND, 0)}"


def finished(message: str, document_id: int | None = None) -> None:
    st.session_state[MANAGE_DONE] = message
    st.session_state[MANAGE_ROUND] = st.session_state.get(MANAGE_ROUND, 0) + 1
    if document_id is not None:
        st.session_state[MANAGE_NEXT] = document_id
    st.rerun()


def show_manage(client: Api) -> None:
    st.title(MANAGE)
    st.caption(
        "Managing a document doesn't let you read it: documents are worked with by ID, "
        "and nothing here shows a title or text. Every change is recorded in the audit trail."
    )
    if MANAGE_NEXT in st.session_state:
        st.session_state[MANAGE_ID] = st.session_state.pop(MANAGE_NEXT)
    done = st.session_state.pop(MANAGE_DONE, None)
    if done:
        st.success(literal(done))
    signed_in = token(st.session_state)
    try:
        show_managed(client, signed_in)
        with st.expander("Upload a new document"):
            show_upload(client, signed_in)
    except NotSignedIn:
        sign_out(st.session_state, notice=SESSION_ENDED)
        st.rerun()
    except ApiError as error:
        st.error(literal(managing.explain(error)))


def show_managed(client: Api, signed_in: str) -> None:
    chosen = st.number_input("Document ID", min_value=1, step=1, value=None, key=MANAGE_ID)
    if chosen is None:
        st.caption("Enter a document's ID to see how it is managed.")
        return
    document_id = int(chosen)
    try:
        document = client.managed(signed_in, document_id)
    except NotSignedIn:
        raise
    except ApiError as error:
        st.error(literal(managing.explain(error)))
        return
    st.dataframe(
        [{"": name, "Value": value} for name, value in managing.facts(document).items()],
        hide_index=True,
    )
    show_grants(client, signed_in, document)
    if document.status == "deleted":
        st.info("This document has been withdrawn. Nobody can read or find it.")
        return
    with st.expander("Give someone access"):
        show_grant_form(client, signed_in, document)
    with st.expander("Change labels"):
        show_label_form(client, signed_in, document)
    with st.expander("Replace with a new version"):
        show_version_form(client, signed_in, document)
    with st.expander("Withdraw"):
        show_withdraw(client, signed_in, document)


def show_grants(client: Api, signed_in: str, document: ManagedDocument) -> None:
    st.markdown("**Grants**")
    if not document.grants:
        st.caption("Nobody holds a grant to this document.")
    for grant, line in zip(document.grants, managing.grant_lines(document), strict=True):
        left, right = st.columns([4, 1])
        left.text(line)
        if right.button("Revoke", key=f"revoke {grant.email}"):
            client.revoke(signed_in, document.id, grant.email)
            finished(f"Revoked access to document {document.id} for {grant.email}.")


def show_grant_form(client: Api, signed_in: str, document: ManagedDocument) -> None:
    with st.form(form_name("grant"), clear_on_submit=True):
        email = st.text_input("Email")
        until = st.date_input("Until the end of (UTC; leave empty for no end)", value=None)
        submitted = st.form_submit_button("Grant")
    if submitted:
        if not email.strip():
            st.error("Enter the email of the person to give access to.")
            return
        client.grant(signed_in, document.id, email.strip(), managing.until_end_of(until or None))
        finished(f"Gave {email.strip()} access to document {document.id}.")


def show_label_form(client: Api, signed_in: str, document: ManagedDocument) -> None:
    with st.form(form_name("labels")):
        classification = st.selectbox(
            "Classification",
            managing.CLASSIFICATIONS,
            index=managing.CLASSIFICATIONS.index(document.classification)
            if document.classification in managing.CLASSIFICATIONS
            else 0,
        )
        department = st.text_input("Department", value=document.department)
        submitted = st.form_submit_button("Save labels")
    if submitted:
        changed = managing.label_changes(document, str(classification), department)
        if not changed:
            st.info("Nothing changed.")
            return
        client.relabel(signed_in, document.id, **changed)
        finished(f"Changed the labels of document {document.id}.")


def show_version_form(client: Api, signed_in: str, document: ManagedDocument) -> None:
    with st.form(form_name("version"), clear_on_submit=True):
        file = st.file_uploader("File", type=list(managing.FILE_TYPES))
        title = st.text_input("New title (leave empty to keep it)")
        submitted = st.form_submit_button("Upload new version")
    if submitted:
        if file is None:
            st.error(managing.NO_FILE)
            return
        new = client.new_version(
            signed_in,
            document.id,
            filename=file.name,
            data=file.getvalue(),
            title=title.strip() or None,
        )
        finished(f"Version {new.version} is document {new.id}; it replaces {document.id}.", new.id)


def show_withdraw(client: Api, signed_in: str, document: ManagedDocument) -> None:
    sure = st.checkbox(
        "Nobody will be able to read or find this document", key=f"withdraw {document.id}"
    )
    if st.button("Withdraw document", disabled=not sure):
        client.delete(signed_in, document.id)
        finished(f"Withdrew document {document.id}.")


def show_upload(client: Api, signed_in: str) -> None:
    with st.form(form_name("upload"), clear_on_submit=True):
        file = st.file_uploader("File", type=list(managing.FILE_TYPES))
        title = st.text_input("Title")
        department = st.text_input("Department", placeholder="such as hr")
        classification = st.selectbox("Classification", managing.CLASSIFICATIONS)
        readers = st.text_area(
            "Readers", help="Emails of the people who may read a restricted document."
        )
        submitted = st.form_submit_button("Upload")
    if not submitted:
        return
    named = managing.readers_from(readers)
    problem = managing.upload_problem(file.name if file else None, title, department, named)
    if problem or file is None:
        st.error(problem or managing.NO_FILE)
        return
    new = client.upload(
        signed_in,
        filename=file.name,
        data=file.getvalue(),
        title=title.strip(),
        department=department.strip(),
        classification=str(classification),
        readers=named,
    )
    finished(f"Uploaded as document {new.id}.", new.id)


# --- the audit trail --------------------------------------------------------------------


def show_audit(client: Api) -> None:
    st.title(AUDIT)
    st.caption("Newest first, in UTC. Every look at this page is itself recorded in the trail.")
    with st.form("audit"):
        left, middle, right = st.columns(3)
        actor = left.text_input("Who (email)")
        action = middle.selectbox(
            "Action", [None, *reviewing.ACTIONS], format_func=lambda a: "Any" if a is None else a
        )
        document_id = right.number_input("Document ID", min_value=1, step=1, value=None)
        first_col, last_col = st.columns(2)
        first = first_col.date_input("From", value=None)
        last = last_col.date_input("To", value=None)
        st.form_submit_button("Show")
    problem = reviewing.window_problem(first or None, last or None)
    if problem:
        st.error(problem)
        return
    since, until = reviewing.window(first or None, last or None)
    try:
        found, page = reviewing.fetch(
            client,
            token(st.session_state),
            actor=actor.strip() or None,
            action=action,
            document_id=None if document_id is None else int(document_id),
            since=since,
            until=until,
            page=int(st.session_state.get("audit_page", 1)),
        )
    except NotSignedIn:
        sign_out(st.session_state, notice=SESSION_ENDED)
        st.rerun()
    except ApiError as error:
        st.error(literal(reviewing.explain(error)))
        return
    if not found.records:
        st.info("No records match.")
        return
    st.caption(f"{found.total} records")
    # Questions are shown in a table, whose cells are plain text.
    st.dataframe(reviewing.rows(found.records), hide_index=True, width="stretch")
    pages = reviewing.pages_for(found.total)
    if pages > 1:
        st.selectbox("Page", list(range(1, pages + 1)), index=page - 1, key="audit_page")


def main() -> None:
    st.set_option("client.showErrorDetails", ERROR_DETAILS)
    st.set_page_config(page_title=TITLE, layout="wide")
    client = api(os.environ.get("UI_API_URL", DEFAULT_API_URL))
    me = current_user(client, st.session_state)
    if me is None:
        show_sign_in(client)
        return
    page = show_sidebar(me)
    if page == ASK:
        show_ask(client)
    elif page == DOCUMENTS:
        show_documents(client)
    elif page == MANAGE:
        show_manage(client)
    elif page == AUDIT:
        show_audit(client)
    else:
        show_home(me)


main()
