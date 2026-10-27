"""compose.tls.yaml and deploy/Caddyfile, read without Docker (docs/deployment.md#tls).

Running them is tested by tests/docker/test_tls.py. These check what a run
wouldn't show at a glance: the proxy is pinned and hardened like the rest,
is the only thing published beyond this machine, keeps no access log and
can't be reconfigured while it runs, and sends each path to the port the
service it names really listens on.
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
TLS = ROOT / "compose.tls.yaml"
CADDYFILE = ROOT / "deploy" / "Caddyfile"


def load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def proxy() -> dict[str, Any]:
    found: dict[str, Any] = load(TLS)["services"]["proxy"]
    return found


def caddyfile() -> str:
    """The Caddyfile without its comments."""
    lines = CADDYFILE.read_text(encoding="utf-8").splitlines()
    return "\n".join(line.split("#", 1)[0] for line in lines)


def test_the_override_adds_the_proxy_and_nothing_else() -> None:
    assert list(load(TLS)["services"]) == ["proxy"]
    assert set(load(TLS)["volumes"]) == {"caddy_data", "caddy_config"}


def test_the_proxy_image_is_pinned_to_a_version_and_a_digest() -> None:
    assert re.fullmatch(r"caddy:\d+\.\d+\.\d+-alpine@sha256:[0-9a-f]{64}", proxy()["image"])


def test_the_proxy_is_hardened_like_every_other_container() -> None:
    found = proxy()

    assert found["read_only"] is True
    assert found["cap_drop"] == ["ALL"]
    # Listening on 80 and 443 needs this one, and nothing else does.
    assert found["cap_add"] == ["NET_BIND_SERVICE"]
    assert found["security_opt"] == ["no-new-privileges:true"]
    assert "privileged" not in found
    assert found["logging"] == load(ROOT / "compose.yaml")["services"]["api"]["logging"]


def test_the_proxy_waits_for_both_to_be_healthy() -> None:
    assert proxy()["depends_on"] == {
        "api": {"condition": "service_healthy"},
        "ui": {"condition": "service_healthy"},
    }


def test_the_proxy_is_the_only_thing_published_beyond_this_machine() -> None:
    # The others publish on 127.0.0.1 only (tests/unit/test_compose.py).
    assert proxy()["ports"] == ["${HTTPS_PORT:-443}:443", "${HTTP_PORT:-80}:80"]


def test_the_proxy_is_given_its_address_and_no_secret() -> None:
    assert proxy()["environment"] == {"SITE_ADDRESS": "${SITE_ADDRESS:-localhost}"}
    assert "secrets" not in proxy()
    assert "./deploy/Caddyfile:/etc/caddy/Caddyfile:ro" in proxy()["volumes"]


def test_nothing_can_reconfigure_the_proxy_while_it_runs() -> None:
    assert re.search(r"^\s*admin off\s*$", caddyfile(), re.MULTILINE)


def test_the_proxy_keeps_no_access_log() -> None:
    # It would hold every URL; the service logs each request without its query.
    assert not re.search(r"^\s*log\b", caddyfile(), re.MULTILINE)


def test_each_path_goes_to_the_port_its_service_listens_on() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    text = caddyfile()

    assert re.search(r"handle_path /api/\* \{\s*reverse_proxy api:8000\s*\}", text)
    assert re.search(r"handle \{\s*reverse_proxy ui:8501\s*\}", text)
    assert '"--port", "8000"' in dockerfile
    assert "--server.port=8501" in dockerfile


def test_browsers_are_told_to_use_https_only() -> None:
    assert 'Strict-Transport-Security "max-age=31536000"' in caddyfile()


def test_the_site_is_the_address_people_use() -> None:
    assert re.search(r"^\{\$SITE_ADDRESS\} \{", caddyfile(), re.MULTILINE)
