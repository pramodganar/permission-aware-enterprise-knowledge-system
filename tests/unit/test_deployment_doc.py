"""docs/deployment.md against what it tells an operator to run.

The backup, the restore and the proxy's authority are the ones the Docker
tests run. Every script it names exists, and the ones run on the machine
itself need nothing but Python. The names it gives, of the volume, the
image and the services, are the stack's, and the exit codes it says to act
on are the scripts' own.
"""

import ast
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from dockertools import BACKUP, PROXY_AUTHORITY, RESTORE

from scripts import audit, index, monitor
from scripts.monitor import Severity

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "deployment.md"
# Run on the machine, not in a container: nothing may be installed for them.
ON_THE_MACHINE = ("make_secrets", "monitor", "scan_secrets")
# The service a docker compose command names.
_SERVICE = re.compile(r"docker compose (?:\S+ )*?(?:stop|start|exec -T|exec|cp) (\w+)")


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def compose(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    return loaded


def test_the_backup_and_the_restore_are_the_ones_the_tests_run() -> None:
    text = page()

    assert " ".join(BACKUP) in text
    assert " ".join(RESTORE) in text
    # Both in the API's image, as root, with no network.
    assert text.count("docker run --rm --user 0 --network none") == 2


def test_the_proxys_authority_is_where_the_tests_take_it_from() -> None:
    assert f"proxy:{PROXY_AUTHORITY}" in page()


def test_the_names_are_the_stacks() -> None:
    text = page()
    stack, tls = compose("compose.yaml"), compose("compose.tls.yaml")

    # Every command that mounts the data names the stack's own volume.
    mounted = re.findall(r"-v (\S+):/data\b", text)
    assert len(mounted) == 2
    assert set(mounted) == {f"{stack['name']}_data"}
    assert stack["services"]["api"]["image"] == "knowledge-api"
    assert " knowledge-api tar " in text
    for service in _SERVICE.findall(text):
        assert service in stack["services"] or service in tls["services"], service


def test_every_script_it_names_exists() -> None:
    named = set(re.findall(r"python3? -m scripts\.(\w+)", page()))

    assert named >= set(ON_THE_MACHINE) | {"audit", "index"}
    for name in named:
        assert (ROOT / "scripts" / f"{name}.py").is_file(), name


@pytest.mark.parametrize("name", ON_THE_MACHINE)
def test_the_scripts_run_on_the_machine_need_only_python(name: str) -> None:
    tree = ast.parse((ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert (ROOT / "scripts" / "__init__.py").read_text(encoding="utf-8").strip() == ""


def row_of(command: str) -> str:
    """The line of the table of what to run, and how often, that runs ``command``."""
    rows = [line for line in page().splitlines() if line.startswith("| ") and command in line]
    assert len(rows) == 1, command
    return rows[0]


def test_the_exit_codes_to_act_on_are_the_scripts() -> None:
    watching = row_of("python3 -m scripts.monitor")

    assert f"{monitor.EXIT_CODES[Severity.PAGE]} a page" in watching
    assert f"{monitor.EXIT_CODES[Severity.TICKET]} a ticket" in watching
    assert f"{monitor.NOTHING_TO_READ} the API writing nothing" in watching
    assert f"{audit.CHANGED}: a record doesn't match" in row_of("scripts.audit check")
    assert f"{index.OUT_OF_DATE}: the index is behind" in row_of("scripts.index status")


def test_the_start_command_uses_both_files() -> None:
    assert "docker compose -f compose.yaml -f compose.tls.yaml up -d --build --wait" in page()
