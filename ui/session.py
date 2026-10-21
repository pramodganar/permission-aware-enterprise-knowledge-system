"""Who is signed in to the UI, kept per browser session.

The state is a plain mapping, Streamlit's session state in the app and a
dict in tests, and it holds exactly two things: the token with when it
stops working, and who the service last said the person is. Never the
password, which is used once and dropped.

Who the person is gets asked again on every interaction, with ``me``. So a
role taken away or an account deactivated takes effect on the person's next
click, as it does in the API. An expired token, or any call that comes back
"not signed in", signs them out with a message saying why.

What the person is told when signing in fails is as vague as the service
is: the same words for a wrong password, an unknown email and a deactivated
account. Only waiting and a service that can't be reached say more, because
those tell the person what to do and nothing about anyone's account.
"""

from collections.abc import MutableMapping
from typing import Any

from ui.client import Api, ApiError, Login, Me, NotSignedIn, TooManyAttempts, Unavailable

LOGIN = "login"
ME = "me"
NOTICE = "notice"

SIGN_IN_FAILED = "Sign-in failed. Check your email and password."
SESSION_ENDED = "Your session has ended. Sign in again."
UNREACHABLE = "The service can't be reached right now. Try again in a moment."
SIGNED_OUT = "You are signed out."

HOME = "Home"
ASK = "Ask and search"

# Streamlit types its keys as str | int, so the key type is left open.
State = MutableMapping[Any, Any]


def sign_in(api: Api, state: State, email: str, password: str) -> str | None:
    """Sign in. Returns what to tell the person if it failed, else None."""
    sign_out(state, notice=None)
    if not email.strip() or not password:
        return "Enter your email and password."
    try:
        login = api.login(email.strip(), password)
        me = api.me(login.token)
    except TooManyAttempts as error:
        minutes = max(1, -(-(error.retry_after or 60) // 60))
        return f"Too many attempts. Try again in {minutes} minutes."
    except Unavailable:
        return UNREACHABLE
    except ApiError:
        # Wrong password, unknown email, deactivated account, a malformed
        # email: all look the same, as they do from the service.
        return SIGN_IN_FAILED
    state[LOGIN] = login
    state[ME] = me
    return None


def current_user(api: Api, state: State) -> Me | None:
    """Who is signed in, checked with the service now. None if nobody is."""
    login: Login | None = state.get(LOGIN)
    if login is None:
        return None
    if login.expired():
        sign_out(state, notice=SESSION_ENDED)
        return None
    try:
        me = api.me(login.token)
    except NotSignedIn:
        sign_out(state, notice=SESSION_ENDED)
        return None
    except Unavailable:
        # Not the person's fault and not a reason to sign them out: show
        # them who they were, and the page says the service is down.
        state[NOTICE] = UNREACHABLE
        cached: Me | None = state.get(ME)
        return cached
    state[ME] = me
    state.pop(NOTICE, None)
    return me


def token(state: State) -> str:
    """The token for a call. Empty if nobody is signed in, which the client refuses."""
    login: Login | None = state.get(LOGIN)
    return login.token if login is not None else ""


def sign_out(state: State, notice: str | None = SIGNED_OUT) -> None:
    """Forget the token and who the person was.

    The token itself stays valid at the service until it expires; tokens
    here can't be withdrawn early (decision record 0004).
    """
    state.pop(LOGIN, None)
    state.pop(ME, None)
    if notice is None:
        state.pop(NOTICE, None)
    else:
        state[NOTICE] = notice


def can_read(me: Me) -> bool:
    return any(p.startswith("documents:read:") for p in me.permissions)


def pages_for(me: Me) -> list[str]:
    """The pages to offer, from the permissions the service gave.

    Only so nobody is offered what they'd be refused: the API is the check.
    """
    pages = [HOME]
    if can_read(me):
        pages.append(ASK)
    return pages


def what_they_can_do(me: Me) -> list[str]:
    """What the account may do, in words, from the permissions the service gave."""
    can = []
    if can_read(me):
        can.append("Ask questions and search the documents you may read.")
    if me.can("documents:manage"):
        can.append("Upload and manage documents. Managing a document doesn't let you read it.")
    if me.can("audit:read"):
        can.append("Review the audit trail.")
    return can or ["Nothing yet: your account has no role that grants access."]
