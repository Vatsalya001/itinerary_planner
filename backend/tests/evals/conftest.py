from __future__ import annotations

import pytest

import app.main as main_module


@pytest.fixture(autouse=True)
def _quiet_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    # The API lifespan (HTTP-layer cases) installs a JSON stdout log handler; keep the test process hermetic.
    monkeypatch.setattr(main_module, "setup_observability", lambda settings: {})
