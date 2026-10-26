"""Every log call in app/, read from the source without running it.

Used by the tests that hold the page of events (docs/logging.md) and the
monitoring rules (scripts/monitor.py) to what the service really writes.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}


@dataclass(frozen=True)
class Call:
    where: str
    logger: str
    # None for logger.log(), whose level is worked out when it runs.
    level: str | None
    message: ast.expr
    fields: tuple[str, ...]
    formatted: bool
    extra: ast.expr | None

    @property
    def text(self) -> str:
        assert isinstance(self.message, ast.Constant), self.where
        assert isinstance(self.message.value, str), self.where
        return self.message.value


def module_of(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def calls() -> list[Call]:
    found = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
                and (node.func.attr in LEVELS or node.func.attr == "log")
            ):
                continue
            args = node.args[1:] if node.func.attr == "log" else node.args
            extra = next((k.value for k in node.keywords if k.arg == "extra"), None)
            keys = extra.keys if isinstance(extra, ast.Dict) else []
            found.append(
                Call(
                    where=f"{path.relative_to(ROOT).as_posix()}:{node.lineno}",
                    logger=module_of(path),
                    level=None if node.func.attr == "log" else node.func.attr.upper(),
                    message=args[0],
                    fields=tuple(str(k.value) for k in keys if isinstance(k, ast.Constant)),
                    formatted=len(args) > 1,
                    extra=extra,
                )
            )
    return found
