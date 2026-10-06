"""CUJ7: configured model discovery in a dedicated unmanaged workspace."""

import os

import pytest
from databricks.sdk.errors import NotFound
from utils.managed import MANAGED_CONFIGS_PATH, assert_no_managed_config
from utils.model_discovery import (
    assert_claude_system_models_in_picker,
    assert_codex_default_models,
)
from utils.terminal import AgentTerminal

from tests.e2e_cuj.base import BaseCujTest

pytestmark = [pytest.mark.live, pytest.mark.cuj7, pytest.mark.workspace_isolated]


class TestUnmanagedModelDiscovery(BaseCujTest):
    WORKSPACE_URL = os.environ.get("UCODE_TEST_WORKSPACE", "").strip().rstrip("/")

    @pytest.fixture(autouse=True)
    def unmanaged_workspace(self, setup_workspace):
        try:
            payload = self.workspace.api_client.do("GET", MANAGED_CONFIGS_PATH)
        except NotFound:
            return
        assert_no_managed_config(payload)

    @pytest.mark.claude
    @pytest.mark.tui
    def test_case_07_configured_claude_discovers_system_models(self, live_session, workspace):
        """Scenario: configure Claude, then launch without source overrides or discovery flags.

        Expected: native discovery caches system.ai models as raw IDs or recognized Claude
        gateway aliases and shows a discovered picker entry.
        """
        session = live_session
        session.run(
            "configure",
            "--agents",
            "claude",
            "--workspace",
            workspace,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        with AgentTerminal(
            session, "claude", [str(session.binary), "claude"], "case-07-system-models"
        ) as tui:
            tui.boot()
            screen = tui.open_model_picker(
                model_visible=lambda _text: session.claude_gateway_cache_ready()
            )
            tui.exit_normally()
        assert_claude_system_models_in_picker(session, screen)

    @pytest.mark.codex
    def test_case_08_configured_codex_uses_default_models(self, live_session, workspace):
        """Scenario: configure Codex, then launch without source overrides.

        Expected: unmanaged configuration leaves model selection to Codex's native default;
        ug records system.ai discovery while model and reasoning preferences remain unset;
        app-server exposes native GPT entries without a generated provider/parent-scoped catalog.
        """
        session = live_session
        session.run(
            "configure",
            "--agents",
            "codex",
            "--workspace",
            workspace,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        models = session.codex_model_ids(["app-server", "--listen", "stdio://"])
        assert_codex_default_models(session, models)
