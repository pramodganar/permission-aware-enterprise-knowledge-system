"""The demo page. Run with: streamlit run ui/streamlit_app.py

Settings come from the environment and .env as everywhere else. On Streamlit
Community Cloud they come from the app's secrets, which are copied into the
environment here. With no model configured the page still works: it shows
the passages each person would get, without answers.
"""

import os
import secrets
import sys
from pathlib import Path

import streamlit as st
from pydantic import SecretStr

# The page is started from ui/, and the application lives one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings
from app.core.exceptions import AppError, TooManyRequestsError
from app.rag.answer import Answer
from app.rag.context import Context, Source
from app.rag.providers import load_provider
from ui.demo import Demo, Person, build_demo
from ui.text import ERROR_DETAILS

# A page anyone with the link can open spends the model's free quota. This
# keeps one visit from using up the budget every visitor shares (ui/demo.py);
# reloading starts a new visit, but not a new budget.
QUESTIONS_PER_VISIT = 30

EXAMPLES = [
    ("david", "How are executive bonuses calculated?"),
    ("grace", "How are executive bonuses calculated?"),
    ("david", "What is the approval limit for vendor payments?"),
    ("bob", "What is the approval limit for vendor payments?"),
    ("frank", "What is the hotel limit when travelling?"),
    ("carol", "Which employees are on a performance improvement plan?"),
]


def settings() -> Settings:
    try:
        for name, value in st.secrets.items():
            # TOML gives numbers as numbers; the settings read them as text.
            if name.startswith("APP_") and isinstance(value, str | int | float):
                os.environ.setdefault(name, str(value))
    except FileNotFoundError:
        # No secrets file: running locally, with .env.
        pass
    if "APP_JWT_SECRET_KEY" in os.environ:
        return Settings()
    # Nobody logs in to the demo, so a key made up for this process will do.
    return Settings(jwt_secret_key=SecretStr(secrets.token_urlsafe(48)))


@st.cache_resource(show_spinner="Loading the company: 17 documents, 9 people, one index...")
def demo() -> Demo:
    current = settings()
    return build_demo(current, load_provider(current))


def label(person: Person) -> str:
    return f"{person.name} ({person.department})"


def show_sources(sources: tuple[Source, ...], heading: str) -> None:
    if not sources:
        return
    st.markdown(f"**{heading}**")
    for source in sources:
        with st.expander(
            f"[{source.label}] {source.document_title}, version {source.document_version}"
        ):
            st.text(source.text)


def show_answer(answer: Answer) -> None:
    if not answer.answered:
        st.info(answer.text)
        return
    st.markdown(answer.text)
    if answer.cut_off:
        st.warning("The answer was cut off at the length limit; the rest is missing.")
    if answer.uncited:
        st.caption("This answer cites no source. It was written from the sources below.")
        show_sources(answer.context.sources, "Given to the model")
    else:
        show_sources(answer.citations, "Sources")


def show_search(context: Context) -> None:
    if context.is_empty:
        st.info("No information available to answer this question.")
        return
    show_sources(context.sources, "Passages this person would get")


def respond(app: Demo, person: Person, question: str) -> None:
    asked = st.session_state.get("asked", 0)
    if asked >= QUESTIONS_PER_VISIT:
        st.warning(
            f"This demo answers {QUESTIONS_PER_VISIT} questions per visit. Reload to start again."
        )
        return
    st.session_state["asked"] = asked + 1
    try:
        if app.answers_on:
            with st.spinner(f"Answering as {person.name}..."):
                show_answer(app.ask(person.key, question))
        else:
            show_search(app.search(person.key, question))
    except TooManyRequestsError as error:
        minutes = -(-error.retry_after // 60)
        st.warning(
            "The demo has answered as many questions as it will for now, for everyone "
            f"together. Try again in {minutes} minute{'s' * (minutes != 1)}. "
            "Questions already answered are still shown."
        )
    except AppError as error:
        # The fixed public message only, as the API will show it.
        st.error(error.detail if error.expose_detail else error.public_message)


st.set_option("client.showErrorDetails", ERROR_DETAILS)
st.set_page_config(page_title="Permission-Aware Knowledge Assistant", layout="wide")
st.title("Permission-Aware Knowledge Assistant")
st.caption(
    "AcmeCorp is a fictional company made up for this project. "
    "Every document and person is invented."
)

app = demo()
people = {person.key: person for person in app.people()}

with st.sidebar:
    st.header("Who are you?")
    st.caption(
        "Choosing a person stands in for logging in. In the service, who is asking "
        "comes from a signed token, never from anything the person types."
    )
    me = people[
        st.selectbox(
            "Ask as",
            list(people),
            index=list(people).index("david"),
            format_func=lambda key: label(people[key]),
        )
    ]
    st.markdown(f"**Roles:** {', '.join(me.roles)}")
    st.markdown(f"**May read {len(me.reads)} of 17 documents:**")
    st.markdown("\n".join(f"- {title}" for title in me.reads) or "_none_")
    if not app.answers_on:
        st.warning("No language model is configured, so this shows passages, not answers.")

ask, compare, how = st.tabs(["Ask", "Compare two people", "How it works"])

with ask:
    st.markdown("Ask anything about AcmeCorp. You get answers only from documents you may read.")
    with st.form("ask"):
        question = st.text_input("Question", placeholder="What is the hotel limit when travelling?")
        submitted = st.form_submit_button("Ask")
    if submitted and question.strip():
        st.markdown(f"**{me.name} asks:** {question}")
        respond(app, me, question)

with compare:
    st.markdown(
        "The same question from two people. "
        "Each answer is built only from what that person may read."
    )
    with st.form("compare"):
        left_key, right_key = st.columns(2)
        first = left_key.selectbox(
            "First person",
            list(people),
            index=list(people).index("david"),
            format_func=lambda key: label(people[key]),
        )
        second = right_key.selectbox(
            "Second person",
            list(people),
            index=list(people).index("grace"),
            format_func=lambda key: label(people[key]),
        )
        shared = st.text_input("Question", value="How are executive bonuses calculated?")
        compared = st.form_submit_button("Ask both")
    if compared and shared.strip():
        left, right = st.columns(2)
        for column, key in ((left, first), (right, second)):
            with column:
                st.markdown(f"#### {people[key].name}")
                respond(app, people[key], shared)

with how:
    st.markdown(
        """
**The rule:** nobody receives information they may not read. It is enforced in code
before the language model runs, never by asking the model.

1. Your question is embedded and searched **only among the chunks you may read**. That list
   comes from the database, for this question, at this moment.
2. Every chunk found is **checked again** by a second copy of the rule.
3. If nothing you may read is relevant, you get "no information" and **the model is not called**.
4. Otherwise the model gets only your passages, labelled S1, S2, and must answer from them,
   or reply that they don't answer the question.
5. Citations are looked up in **your** passages only, so they can't point anywhere else.

Try the examples: David (Sales) and Grace (an executive) asking about executive bonuses,
or David and Bob (Finance) asking about vendor payment limits.
"""
    )
    st.markdown("**Example questions**")
    for key, example in EXAMPLES:
        st.markdown(f"- {people[key].name}: _{example}_")
    st.caption("Source code and documentation: see the project repository.")
