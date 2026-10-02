"""Shared fixtures: the failure limiters are process-wide singletons, so reset
them around every test to keep tests independent."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_limiters():
    from src import ratelimit

    ratelimit.reset_all()
    yield
    ratelimit.reset_all()
