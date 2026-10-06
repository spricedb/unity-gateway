"""Reuse the installed-agent harness with SDK workspace authentication."""

import os
import shutil

import pytest

from tests.integration.conftest import installed_binary as installed_binary
from tests.integration.conftest import (
    pytest_collection_modifyitems as pytest_collection_modifyitems,
)
from tests.integration.conftest import session as session


@pytest.fixture
def workspace(request, setup_workspace):
    return request.instance.workspace.config.host


@pytest.fixture
def live_session(session, request, setup_workspace):
    for binary in ["databricks", *os.environ["UG_INTEGRATION_AGENTS"].split(",")]:
        if not shutil.which(binary, path=session.env["PATH"]):
            pytest.fail(f"Required integration binary is missing: {binary}")
    session.env["DATABRICKS_BEARER"] = request.instance.workspace.config.authenticate()[
        "Authorization"
    ].removeprefix("Bearer ")
    return session
