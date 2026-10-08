import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from pydantic import SecretStr

from app.auth.tokens import ALGORITHM, create_access_token, decode_access_token
from app.core.config import Settings
from app.core.exceptions import AuthenticationError, error_response

SECRET = "test-secret-" + "x" * 60
OTHER_SECRET = "other-secret-" + "y" * 60


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, jwt_secret_key=SecretStr(SECRET))


def sign(claims: dict[str, Any], secret: str = SECRET, algorithm: str = ALGORITHM) -> str:
    return jwt.encode(claims, secret, algorithm=algorithm)


def valid_claims(**changes: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {"sub": "7", "iat": now, "exp": now + timedelta(minutes=5)}
    claims.update(changes)
    return claims


def b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_token_round_trip_returns_the_user_id(settings: Settings) -> None:
    assert decode_access_token(create_access_token(7, settings), settings) == 7


def test_token_carries_identity_and_expiry_only(settings: Settings) -> None:
    token = create_access_token(7, settings)

    claims = jwt.decode(token, options={"verify_signature": False})

    assert set(claims) == {"sub", "iat", "exp"}
    assert claims["sub"] == "7"


def test_token_expires_after_the_configured_time(settings: Settings) -> None:
    token = create_access_token(7, settings)

    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["exp"] - claims["iat"] == settings.access_token_expire_minutes * 60


def test_expired_token_is_rejected(settings: Settings) -> None:
    issued = datetime.now(UTC) - timedelta(minutes=settings.access_token_expire_minutes + 1)
    token = create_access_token(7, settings, now=issued)

    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)


def test_token_signed_with_another_secret_is_rejected(settings: Settings) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(sign(valid_claims(), secret=OTHER_SECRET), settings)


def test_changing_the_user_id_breaks_the_signature(settings: Settings) -> None:
    header, _, signature = create_access_token(7, settings).split(".")
    forged_payload = b64({"sub": "1", "iat": 0, "exp": 9999999999})

    with pytest.raises(AuthenticationError):
        decode_access_token(f"{header}.{forged_payload}.{signature}", settings)


def test_unsigned_token_is_rejected(settings: Settings) -> None:
    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64({"sub": "7", "iat": 0, "exp": 9999999999})

    for token in (f"{header}.{payload}.", f"{header}.{payload}"):
        with pytest.raises(AuthenticationError):
            decode_access_token(token, settings)


def test_token_signed_with_a_different_algorithm_is_rejected(settings: Settings) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(sign(valid_claims(), algorithm="HS512"), settings)


@pytest.mark.parametrize("claim", ["sub", "iat", "exp"])
def test_token_missing_a_required_claim_is_rejected(settings: Settings, claim: str) -> None:
    claims = valid_claims()
    del claims[claim]

    with pytest.raises(AuthenticationError):
        decode_access_token(sign(claims), settings)


@pytest.mark.parametrize("subject", ["alice", "", "7; DROP TABLE users", "1.5"])
def test_token_whose_subject_is_not_a_user_id_is_rejected(settings: Settings, subject: str) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(sign(valid_claims(sub=subject)), settings)


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c", "Bearer abc"])
def test_garbage_is_rejected(settings: Settings, token: str) -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings)


def test_every_token_failure_gives_the_client_the_same_response(settings: Settings) -> None:
    def response_for(token: str) -> object:
        with pytest.raises(AuthenticationError) as error:
            decode_access_token(token, settings)
        return error_response(error.value)

    expired = create_access_token(7, settings, now=datetime.now(UTC) - timedelta(days=2))
    forged = sign(valid_claims(), secret=OTHER_SECRET)

    assert response_for(expired) == response_for(forged) == response_for("garbage")


def test_failure_detail_names_the_cause_for_the_log(settings: Settings) -> None:
    expired = create_access_token(7, settings, now=datetime.now(UTC) - timedelta(days=2))

    with pytest.raises(AuthenticationError) as error:
        decode_access_token(expired, settings)

    assert "ExpiredSignatureError" in error.value.detail


def test_secret_is_not_in_the_token(settings: Settings) -> None:
    assert SECRET not in create_access_token(7, settings)
