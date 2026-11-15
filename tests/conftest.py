"""Shared pytest checks for the whole suite."""

import pytest

# Windows limits an environment variable to 32,767 characters, and pytest
# stores the running test's name in PYTEST_CURRENT_TEST. Long parameter values
# (older pytest versions copy them into the name) break the run on Windows
# while passing on Linux (RL-75). Keep names short by giving such parameters
# an explicit id with pytest.param(..., id="short-name").
MAX_TEST_NAME_LENGTH = 500


def pytest_collection_modifyitems(items):
    too_long = [item.nodeid[:120] + "..." for item in items
                if len(item.nodeid) > MAX_TEST_NAME_LENGTH]
    if too_long:
        raise pytest.UsageError(
            f"Test names longer than {MAX_TEST_NAME_LENGTH} characters break "
            "test runs on Windows. Give these parameters a short "
            "pytest.param(..., id=...):\n" + "\n".join(too_long)
        )
