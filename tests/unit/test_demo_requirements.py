"""The demo page's requirements, as Streamlit Community Cloud installs them."""

import re
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def pins(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found = (re.fullmatch(r"([A-Za-z0-9_.-]+)==(\S+)", line.strip()) for line in lines)
    return {m.group(1).lower(): m.group(2) for m in found if m}


def test_the_demo_installs_everything_the_application_needs() -> None:
    lines = (ROOT / "ui" / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert "-r ../requirements.txt" in lines


def test_the_demo_takes_pytorch_from_the_cpu_only_index() -> None:
    lines = (ROOT / "ui" / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in lines
    # The version that is installed and tested here.
    assert pins(ROOT / "ui" / "requirements.txt")["torch"] == version("torch").split("+")[0]


def test_streamlit_is_pinned_the_same_for_the_demo_and_for_development() -> None:
    demo = pins(ROOT / "ui" / "requirements.txt")["streamlit"]

    assert demo == pins(ROOT / "requirements-dev.txt")["streamlit"]
    assert demo == version("streamlit")
