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

from ui.client import DEFAULT_API_URL, Api, Me
from ui.session import NOTICE, current_user, sign_in, sign_out, what_they_can_do

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


def show_sidebar(me: Me) -> None:
    with st.sidebar:
        st.subheader(me.full_name)
        st.caption(f"{me.department.name} · {', '.join(me.roles) or 'no role'}")
        if st.button("Sign out"):
            sign_out(st.session_state)
            st.rerun()


def show_home(me: Me) -> None:
    st.title(TITLE)
    notice = st.session_state.get(NOTICE)
    if notice:
        st.warning(notice)
    st.write(f"Signed in as **{me.email}**.")
    st.markdown("\n".join(f"- {line}" for line in what_they_can_do(me)))


def main() -> None:
    st.set_page_config(page_title=TITLE, layout="wide")
    client = api(os.environ.get("UI_API_URL", DEFAULT_API_URL))
    me = current_user(client, st.session_state)
    if me is None:
        show_sign_in(client)
        return
    show_sidebar(me)
    show_home(me)


main()
