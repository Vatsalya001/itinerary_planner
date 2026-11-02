from __future__ import annotations

from pathlib import Path

import pytest

import app.main as main_module
import evals.runner as runner_module


@pytest.fixture(autouse=True)
def _quiet_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    # The API lifespan (HTTP-layer cases) installs a JSON stdout log handler; keep the test process hermetic.
    monkeypatch.setattr(main_module, "setup_observability", lambda settings: {})


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    # The runner reads (and its CLI exports) backend/.env; a developer's real keys must never reach these tests.
    monkeypatch.setattr(runner_module, "ENV_FILE", Path(tmp_path_factory.getbasetemp()) / "no-dotenv" / ".env")
