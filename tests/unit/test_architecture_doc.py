"""docs/architecture.md describes the code as it is, not as it was planned.

It began as a plan written before any code, and by the end of the build its
diagram showed a reranker in the middle of every question and an admin
client that never existed. These keep the diagrams true: the components
diagram names every package under ``app/`` and its arrows are exactly the
imports between them, the deployment diagram shows every service Compose
starts, every file the page names exists, every threat it names is in the
threat model, and the number of routes it gives is the number there are.
"""

import ast
import re

import yaml
from docrefs import ROOT

PAGE = ROOT / "docs" / "architecture.md"
APP = ROOT / "app"
# Used by every package, so the diagram gives them no arrows.
EVERYWHERE = {"core", "models"}
# Nodes of the deployment diagram that are not a Compose service.
NOT_SERVICES = {"person", "data", "secrets", "llm", "log"}

# A diagram is found by the comment under its first line, such as %% components.
_BLOCK = re.compile(r"```mermaid\n\w+ \w+\n%% (\w+)\n(.*?)```", re.DOTALL)
_NODE = re.compile(r'^\s*(\w+)[\[(/]+"', re.MULTILINE)
_LABELLED = re.compile(r'^\s*(\w+)\["(app/\w+)<br/>', re.MULTILINE)
_EDGE = re.compile(r"^\s*(\w+) -->(?:\|[^|]*\|)? (\w+)\s*$", re.MULTILINE)
_PATH = re.compile(r"`((?:app|ui|scripts|evaluation|tests|deploy)/[\w./]+)`")


def diagram(name: str) -> str:
    blocks: dict[str, str] = dict(_BLOCK.findall(PAGE.read_text(encoding="utf-8")))
    assert name in blocks, f"no mermaid block marked %% {name}"
    return blocks[name]


def packages() -> set[str]:
    return {p.parent.name for p in APP.glob("*/__init__.py")}


def imports_between_packages() -> set[tuple[str, str]]:
    edges = set()
    for path in APP.glob("*/**/*.py"):
        source = path.relative_to(APP).parts[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                parts = module.split(".")
                if parts[0] == "app" and len(parts) > 1 and parts[1] != source:
                    edges.add((source, parts[1]))
    return {(a, b) for a, b in edges if not {a, b} & EVERYWHERE}


def test_the_components_diagram_names_every_package_by_its_own_node() -> None:
    labelled = dict(_LABELLED.findall(diagram("components")))

    assert {f"app/{name}" for name in packages()} == set(labelled.values())
    for node, path in labelled.items():
        assert path == f"app/{node}", f"node {node} is labelled {path}"


def test_the_arrows_are_exactly_the_imports_between_packages() -> None:
    drawn = {(a, b) for a, b in _EDGE.findall(diagram("components")) if a in packages()}
    actual = imports_between_packages()

    assert drawn - actual == set(), "drawn, but no such import"
    assert actual - drawn == set(), "imported, but not drawn"


def test_the_retrieval_arrows_are_the_ones_the_rule_depends_on() -> None:
    # The two the page builds its argument on: retrieval asks authorization,
    # and the model is reached only through retrieval.
    actual = imports_between_packages()

    assert ("retrieval", "authorization") in actual
    assert ("rag", "retrieval") in actual
    assert not any(a == "rag" and b == "ingestion" for a, b in actual)


def test_the_deployment_diagram_shows_every_service_compose_starts() -> None:
    services = set()
    for file in ("compose.yaml", "compose.tls.yaml"):
        services |= set(yaml.safe_load((ROOT / file).read_text(encoding="utf-8"))["services"])
    nodes = set(_NODE.findall(diagram("deployment")))

    assert services, "no services read"
    assert nodes - NOT_SERVICES == services


def test_every_file_the_page_names_exists() -> None:
    named = set(_PATH.findall(PAGE.read_text(encoding="utf-8")))

    assert len(named) > 20
    for path in named:
        assert (ROOT / path.rstrip("/")).exists(), path


def test_every_threat_the_page_names_is_in_the_threat_model() -> None:
    model = (ROOT / "docs" / "threat-model.md").read_text(encoding="utf-8")
    threats = set(re.findall(r"^\| (T\d+) \|", model, re.MULTILINE))
    named = set(re.findall(r"\bT\d+\b", PAGE.read_text(encoding="utf-8")))

    assert named
    assert named <= threats, sorted(named - threats)


def test_the_number_of_routes_is_the_number_there_are() -> None:
    routes = 0
    for path in (APP / "api").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        routes += len(re.findall(r"^@router\.(?:get|post|put|patch|delete)\(", text, re.MULTILINE))
    said = re.search(r"The (\d+) routes are listed", PAGE.read_text(encoding="utf-8"))

    assert said, "the page no longer says how many routes there are"
    assert int(said[1]) == routes
