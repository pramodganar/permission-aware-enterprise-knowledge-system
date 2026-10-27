"""The stack behind the TLS proxy of compose.tls.yaml (docs/deployment.md#tls).

For localhost the proxy makes its own certificate authority, so the tests
trust that authority, copied out of the proxy, and nothing else: a
certificate that didn't match the name, or wasn't the proxy's, fails.
"""

import socket
import ssl
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from dockertools import PROXY_AUTHORITY
from test_stack import Stack

pytestmark = pytest.mark.docker


class Front:
    def __init__(self, stack: Stack, trusted: ssl.SSLContext) -> None:
        self.stack = stack
        self.trusted = trusted
        self.url = f"https://localhost:{stack.https_port}"

    def get(self, path: str) -> httpx.Response:
        return httpx.get(f"{self.url}{path}", verify=self.trusted, timeout=30)


def authority(stack: Stack, folder: Path) -> ssl.SSLContext:
    copied = folder / "root.crt"
    stack.compose("cp", f"proxy:{PROXY_AUTHORITY}", str(copied))
    return ssl.create_default_context(cafile=str(copied))


@pytest.fixture(scope="module")
def front(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Front]:
    folder = tmp_path_factory.mktemp("tls")
    stack = Stack(folder, tls=True)
    try:
        stack.up()
        yield ready(stack, folder)
    finally:
        stack.down("-v")


def ready(stack: Stack, folder: Path, seconds: float = 60) -> Front:
    """The proxy once it answers. It has no health check, and makes its
    authority and certificate as it starts."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            found = Front(stack, authority(stack, folder))
            found.get("/api/health")
            return found
        except (AssertionError, OSError, ssl.SSLError, httpx.HTTPError):
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def test_the_api_answers_over_tls_under_api(front: Front) -> None:
    answered = front.get("/api/health")

    assert answered.status_code == 200
    assert answered.json() == {"status": "ok"}


def test_the_ui_answers_over_tls(front: Front) -> None:
    assert front.get("/_stcore/health").text == "ok"
    assert "<html" in front.get("/").text.lower()


def test_the_uis_websocket_goes_through(front: Front) -> None:
    # Streamlit's page talks to its server over a websocket: without it the
    # page loads and does nothing.
    port = front.stack.https_port
    with (
        socket.create_connection(("127.0.0.1", port), timeout=30) as raw,
        front.trusted.wrap_socket(raw, server_hostname="localhost") as tls,
    ):
        tls.sendall(
            (
                "GET /_stcore/stream HTTP/1.1\r\n"
                f"Host: localhost:{port}\r\n"
                f"Origin: https://localhost:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        head = tls.recv(1024)

    assert head.startswith(b"HTTP/1.1 101"), head[:80]


def test_signing_in_works_through_the_proxy(front: Front) -> None:
    login = httpx.post(
        f"{front.url}/api/auth/login",
        json={"email": "david@acmecorp.example", "password": front.stack.password},
        verify=front.trusted,
    )

    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"


def test_a_certificate_for_another_name_is_refused(front: Front) -> None:
    # The same proxy, asked for by a name its certificate isn't for.
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"https://127.0.0.1:{front.stack.https_port}/api/health", verify=front.trusted)


def test_plain_http_is_sent_to_https(front: Front) -> None:
    answered = httpx.get(f"http://localhost:{front.stack.http_port}/api/health")

    assert answered.status_code == 308
    assert answered.headers["Location"].startswith("https://localhost")


def test_browsers_are_told_to_come_back_over_https_only(front: Front) -> None:
    headers = front.get("/api/health").headers

    assert headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "server" not in headers
    # The service's own headers come through the proxy.
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_only_the_proxy_is_reachable_from_beyond_this_machine(front: Front) -> None:
    for service, beyond in (("proxy", True), ("api", False), ("ui", False)):
        host = front.stack.container(service)["HostConfig"]
        assert isinstance(host, dict)
        addresses = {b["HostIp"] for ports in host["PortBindings"].values() for b in ports}
        assert ("127.0.0.1" not in addresses) is beyond, f"{service}: {addresses}"


def test_the_proxy_runs_hardened(front: Front) -> None:
    host = front.stack.container("proxy")["HostConfig"]
    assert isinstance(host, dict)

    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    # Docker writes it with its kernel prefix, CAP_.
    assert [cap.removeprefix("CAP_") for cap in host["CapAdd"]] == ["NET_BIND_SERVICE"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
