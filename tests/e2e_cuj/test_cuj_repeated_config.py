"""CUJ 6: repeated configuration, mixed sources, and removal (AIGTWY-4881).

An existing user receives three successive admin configs in one home. Each change must take
effect without leaving stale ug-managed values or touching the user's own settings.

Prerequisites in the dedicated workspace: the shared ``ug_e2e`` fixtures (the Bedrock Claude
MPS ``ug_e2e.providers.bedrock``, the Codex model services in ``ug_e2e.models``, and the
``fixture_reader`` MCP), workspace tracing, and a SQL warehouse. The CUJ owns the workspace's
managed config and republishes Phase A first on every run.
"""

import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e_cuj.base import BaseCujTest
from tests.integration.utils.evidence import FileTask, agent_sessions
from tests.integration.utils.harness import UserSession
from tests.integration.utils.managed import MANAGED_CONFIGS_PATH
from tests.integration.utils.sql import query_count, resolve_trace_table, resolve_warehouse_id
from tests.integration.utils.terminal import AgentTerminal, TerminalProcess

CLAUDE = "CODING_AGENT_CLAUDE_CODE"
CODEX = "CODING_AGENT_CODEX"
UPDATE_MASK = "default_agent,enabled_agents,mcp_servers,skills,smart_defaults"

CLAUDE_STATIC_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
CODEX_SOL = "system.ai.gpt-5-6-sol"
CODEX_LUNA = "system.ai.gpt-5-6-luna"
CODEX_UC_MODEL = "ug_e2e.models.codex_primary"
CLAUDE_PROVIDER = "ug_e2e.providers.bedrock"
CLAUDE_PROVIDER_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
CODEX_MODEL_LOCATION = "ug_e2e.models"
MANAGED_MCP = "ug_e2e.tools.fixture_reader"
MANAGED_MCP_ENTRY = MANAGED_MCP.replace(".", "-")
USER_MCP = "ug-e2e-user-echo"
CLAUDE_OS_MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")
CODEX_OS_MANAGED_CONFIG = Path("/etc/codex/managed_config.toml")
OS_MANAGED_FILES = (CLAUDE_OS_MANAGED_SETTINGS, CODEX_OS_MANAGED_CONFIG)
USER_SKILL = "ug-e2e-user-notes"
CLAUDE_CUSTOM_HEADER_NAMES = ("X-UG-E2E-Run", "X-UG-E2E-Agent")
CLAUDE_TRACE_ENV_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS",
    "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
)
TRACE_DEADLINE_SECONDS = 300
TRACE_INGESTION_SECONDS = 300

USER_MCP_SERVER = """\
import json, sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ug-e2e-user-echo", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


def phase_a(run_id: str) -> dict:
    """CUJ 1's config plus CUJ 2's named MCP selector."""
    return {
        "spec_version": 1,
        "default_agent": CLAUDE,
        "enabled_agents": [
            {
                "agent": CLAUDE,
                "config": {
                    "models": {"model_services": CLAUDE_STATIC_MODELS},
                    "default_models": {
                        "default_model": "system.ai.claude-sonnet-4-6",
                        "default_opus_model": "system.ai.claude-opus-4-8",
                        "default_sonnet_model": "system.ai.claude-sonnet-4-6",
                        "default_haiku_model": "system.ai.claude-haiku-4-5",
                    },
                    "smart_routing": {"enabled": False},
                    "http_headers": {"X-UG-E2E-Run": run_id, "X-UG-E2E-Agent": "claude"},
                    "tracing": {"enabled": True},
                },
            },
            {
                "agent": CODEX,
                "config": {
                    "models": {"model_services": [CODEX_SOL, CODEX_LUNA]},
                    "default_models": {"default_model": CODEX_SOL},
                    "smart_routing": {"enabled": False},
                    "http_headers": {"X-UG-E2E-Run": run_id, "X-UG-E2E-Agent": "codex"},
                    "tracing": {"enabled": True},
                },
            },
        ],
        "mcp_servers": {"names": [MANAGED_MCP]},
    }


def phase_b(run_id: str) -> dict:
    return {
        "spec_version": 1,
        "default_agent": CODEX,
        "enabled_agents": [
            {
                "agent": CLAUDE,
                "config": {
                    "models": {"model_provider_service": CLAUDE_PROVIDER},
                    "default_models": {"default_model": CLAUDE_PROVIDER_MODEL},
                    "smart_routing": {"enabled": False},
                    "tracing": {"enabled": False},
                },
            },
            {
                "agent": CODEX,
                "config": {
                    "models": {"unity_catalog_location": CODEX_MODEL_LOCATION},
                    "default_models": {"default_model": CODEX_UC_MODEL},
                    "smart_routing": {"enabled": False},
                    "http_headers": {"X-UG-E2E-Run": f"{run_id}-phase-b"},
                    "tracing": {"enabled": True},
                },
            },
        ],
    }


def phase_c() -> dict:
    return {
        "spec_version": 1,
        "default_agent": CODEX,
        "enabled_agents": [
            {
                "agent": CODEX,
                "config": {
                    "models": {"model_services": [CODEX_LUNA]},
                    "default_models": {"default_model": CODEX_LUNA},
                    "smart_routing": {"enabled": False},
                    "tracing": {"enabled": False},
                },
            }
        ],
    }


@dataclass(frozen=True)
class SeededUserState:
    claude_settings: str
    codex_keys: dict
    mcp_registrations: dict[str, dict]
    skill_files: dict[str, str]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text()) if path.is_file() else {}


def prune_defaults(value):
    """Drop false/empty fields, which the API may omit from its JSON readback."""
    if isinstance(value, dict):
        pruned = {key: prune_defaults(item) for key, item in value.items()}
        return {key: item for key, item in pruned.items() if item not in (False, None, {}, [])}
    if isinstance(value, list):
        return [prune_defaults(item) for item in value]
    return value


def model_leaf(model: str) -> str:
    """The model's family id, without catalog/vendor/region prefixes, [1m], date, or version."""
    name = model.rsplit(".", 1)[-1].removesuffix("[1m]")
    return re.sub(r"(-\d{8})?(-v\d+:\d+)?$", "", name)


class TestCujRepeatedConfig(BaseCujTest):
    # Dedicated workspace ug_configure_e2e_cuj_6_DO_NOT_USE.
    WORKSPACE_URL = "https://dbc-8c38ce9b-634b.cloud.databricks.com"

    @pytest.fixture(autouse=True)
    def user_session(self, setup_workspace):
        binary = shutil.which("ug")
        if not binary:
            pytest.fail("Install the ug under test on PATH.", pytrace=False)
        # Codex rejects helper installation beneath /tmp.
        runs = Path.home() / ".cache" / "ug-cuj"
        runs.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(prefix="cuj6-", dir=runs) as root,
            tempfile.TemporaryDirectory(prefix="cuj6-project-", dir=runs) as project,
        ):
            self.run_id = f"cuj6-{uuid.uuid4().hex[:12]}"
            artifacts = runs / f"artifacts-{self.run_id}"
            session = UserSession(Path(root), Path(project), Path(binary), artifacts)
            present = [str(path) for path in OS_MANAGED_FILES if path.exists()]
            if present:
                pytest.fail(f"OS-managed agent settings already exist: {present}", pytrace=False)
            self.session = session
            self.refresh_bearer()
            try:
                yield session
            finally:
                # A terminal launch writes OS-managed files, which only a terminal revert restores.
                if any(
                    (session.home / ".ucode" / name).is_file()
                    for name in ("state.json", "managed-backups/manifest.json")
                ):
                    with TerminalProcess(
                        session, "ug", [str(session.binary), "revert"], "cleanup-revert"
                    ) as terminal:
                        terminal.finish()

    def refresh_bearer(self) -> None:
        """Mint a fresh SP token; the journey outlives a single M2M token."""
        authorization = self.workspace.config.authenticate()["Authorization"]
        self.session.env["DATABRICKS_BEARER"] = authorization.removeprefix("Bearer ")

    def fetch_policy(self) -> dict:
        result = self.workspace.api_client.do("GET", MANAGED_CONFIGS_PATH)
        assert isinstance(result, dict), result
        configs = result.get("coding_agent_configs", [])
        assert len(configs) <= 1, "CUJ 6 requires at most one CodingAgentConfig"
        return configs[0] if configs else {}

    def publish(self, phase: str, config: dict) -> None:
        current = self.fetch_policy()
        if current:
            body = {"name": current["name"], **config}
            self.workspace.api_client.do(
                "PATCH",
                f"/api/ai-gateway/v2/{current['name']}",
                query={"update_mask": UPDATE_MASK},
                body=body,
            )
        else:
            self.workspace.api_client.do("POST", MANAGED_CONFIGS_PATH, body=config)
        deadline = time.monotonic() + 60
        while not self.is_published(config):
            assert time.monotonic() < deadline, "Published config did not become visible"
            time.sleep(1)
        self.session.record(f"published-config-{phase}.json", self.fetch_policy())

    def is_published(self, config: dict) -> bool:
        actual = self.fetch_policy()
        return all(
            prune_defaults(actual.get(key)) == prune_defaults(config.get(key))
            for key in ("default_agent", "enabled_agents", "mcp_servers", "skills")
        )

    @property
    def home(self) -> Path:
        return self.session.home

    def configure(self, *, terminal: bool) -> None:
        self.refresh_bearer()
        args = ["configure", "--workspace", self.WORKSPACE_URL, "--skip-upgrade"]
        args.append("--disable-databricks-ai-tools")
        if not terminal:
            self.session.run(*args, timeout=300)
            return
        with TerminalProcess(
            self.session, "ug", [str(self.session.binary), *args], "configure-terminal"
        ) as process:
            process.finish(timeout=300)

    def claude_env(self) -> dict:
        return read_json(self.home / ".claude/ucode-settings.json").get("env", {})

    def claude_headers(self) -> dict[str, str]:
        lines = self.claude_env().get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
        return dict(line.split(": ", 1) for line in lines if ": " in line)

    def codex_headers(self) -> dict[str, str]:
        ucode = read_toml(self.home / ".codex/ucode.config.toml")
        return ucode.get("model_providers", {}).get("Databricks", {}).get("http_headers", {})

    def registrations(self) -> dict[str, dict]:
        claude = read_json(self.home / ".claude/.claude.json").get("mcpServers", {})
        codex = read_toml(self.home / ".codex/config.toml").get("mcp_servers", {})
        return {"claude": claude, "codex": codex}

    def mcp_list(self, agent: str) -> list[str]:
        """Effective registrations as each agent reports them."""
        binary = shutil.which(agent, path=self.session.env["PATH"])
        if agent == "codex":
            output = self.session.run("mcp", "list", "--json", binary=binary, timeout=120).stdout
            names = [server["name"] for server in json.loads(output)]
        else:
            output = self.session.run("mcp", "list", binary=binary, timeout=120).stdout
            # `name: <command or url> - <status>`; prose lines contain spaces before the colon.
            names = []
            for line in output.splitlines():
                name, separator, _ = line.strip().partition(":")
                is_server_row = bool(separator and name) and " " not in name
                if is_server_row:
                    names.append(name)
        assert len(names) == len(set(names)), f"Duplicate {agent} MCP registrations:\n{output}"
        return names

    def skill_files(self) -> dict[str, str]:
        files = {}
        for root in (self.home / ".claude/skills", self.home / ".agents/skills"):
            for path in sorted(root.rglob("*")) if root.is_dir() else []:
                if path.is_file():
                    files[str(path.relative_to(self.home))] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
        return files

    def effective_settings(self) -> dict:
        """Settings and registrations a repeat configure must not change.

        ug's own state, caches, and Claude's runtime counters in .claude.json are excluded.
        """
        return {
            "claude_ucode_settings": read_json(self.home / ".claude/ucode-settings.json"),
            "claude_user_settings": read_json(self.home / ".claude/settings.json"),
            "codex_ucode_config": read_toml(self.home / ".codex/ucode.config.toml"),
            "codex_config": read_toml(self.home / ".codex/config.toml"),
            "codex_model_catalog": read_json(self.home / ".ucode/codex-model-catalog.json"),
            "registrations": self.registrations(),
            "skill_files": self.skill_files(),
            "claude_mcp_list": sorted(self.mcp_list("claude")),
            "codex_mcp_list": sorted(self.mcp_list("codex")),
            "os_managed_claude": read_json(CLAUDE_OS_MANAGED_SETTINGS),
            "os_managed_codex": read_toml(CODEX_OS_MANAGED_CONFIG),
        }

    def assert_served(self, models: set[str], expected: str) -> None:
        """Every new transcript turn ran on ``expected``, allowing the gateway's id spelling."""
        assert models and all(model_leaf(expected) in model for model in models), (
            f"Expected {expected}, transcripts recorded {models}"
        )

    def served_models(self, agent: str, before: set[str]) -> set[str]:
        """Models in transcripts written after ``before``: Claude's served, Codex's requested."""
        models = set()
        for name, records in agent_sessions(self.session, agent).items():
            if name in before:
                continue
            for record in records:
                if agent == "claude" and record.get("type") == "assistant":
                    models.add(record.get("message", {}).get("model"))
                if agent == "codex" and record.get("type") == "turn_context":
                    models.add(record.get("payload", {}).get("model"))
        models.discard(None)
        return models

    def claude_task(self, *, marker: str | None) -> set[str]:
        before = set(agent_sessions(self.session, "claude"))
        task = FileTask(self.session)
        env = dict(self.session.env)
        if marker:
            self.session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={marker}"
        try:
            result = self.session.run(
                "claude",
                "--",
                "-p",
                task.prompt,
                "--output-format",
                "json",
                "--allowedTools",
                "Read",
                timeout=240,
            )
        finally:
            self.session.env = env
        task.assert_headless_answer("claude", result)
        return self.served_models("claude", before)

    def codex_task(self, *, marker: str | None) -> set[str]:
        before = set(agent_sessions(self.session, "codex"))
        task = FileTask(self.session)
        overrides = (
            ["--config", f'otel.span_attributes.ug_integration_marker="{marker}"'] if marker else []
        )
        result = self.session.run(
            "codex",
            "--",
            *overrides,
            "exec",
            "--skip-git-repo-check",
            "--json",
            task.prompt,
            timeout=240,
        )
        task.assert_headless_answer("codex", result)
        return self.served_models("codex", before)

    def bare_ug_task(self, label: str) -> set[str]:
        """Bare `ug` in a real terminal; the managed default agent must be Codex."""
        before = set(agent_sessions(self.session, "codex"))
        task = FileTask(self.session)
        with AgentTerminal(self.session, "codex", [str(self.session.binary)], label) as tui:
            tui.boot()
            tui.submit(task.prompt)
            tui.wait_for_task(task)
            tui.exit_normally()
        task.assert_completed(self.session, "codex")
        return self.served_models("codex", before)

    def trace_count(self, marker: str, *, resource: bool) -> int:
        attributes = "resource.attributes" if resource else "attributes"
        return query_count(
            self.WORKSPACE_URL,
            self.session.env["DATABRICKS_BEARER"],
            self.warehouse_id,
            f"SELECT COUNT(*) FROM {self.trace_table} "
            "WHERE time > current_timestamp() - INTERVAL 2 HOURS "
            f"AND variant_get({attributes}, '$[\"ug_integration_marker\"]', 'STRING') = :marker",
            [{"name": "marker", "value": marker, "type": "STRING"}],
        )

    def wait_for_trace(self, marker: str) -> None:
        deadline = time.monotonic() + TRACE_DEADLINE_SECONDS
        while self.trace_count(marker, resource=False) == 0:
            assert time.monotonic() < deadline, f"No Codex span for {marker}"
            time.sleep(15)

    def seed_user_state(self) -> SeededUserState:
        """Ordinary user-owned prefs, MCP registration, and skill; no ug state."""
        claude_settings = self.home / ".claude/settings.json"
        claude_settings.parent.mkdir(parents=True)
        claude_settings.write_text(
            json.dumps({"cleanupPeriodDays": 45, "env": {"UG_E2E_USER_PREF": self.run_id}})
        )
        codex_config = self.home / ".codex/config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "hide_agent_reasoning = true\n\n"
            f"[projects.{json.dumps(str(self.session.cwd))}]\n"
            'trust_level = "trusted"\n'
        )
        server = self.home / "user-mcp" / "echo.py"
        server.parent.mkdir()
        server.write_text(USER_MCP_SERVER)
        for agent, args in (
            ("claude", ["mcp", "add", "-s", "user", USER_MCP, "--", sys.executable, str(server)]),
            ("codex", ["mcp", "add", USER_MCP, "--", sys.executable, str(server)]),
        ):
            self.session.run(*args, binary=shutil.which(agent, path=self.session.env["PATH"]))
        for root in (".claude/skills", ".agents/skills"):
            skill = self.home / root / USER_SKILL / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                f"---\nname: {USER_SKILL}\ndescription: Notes the user wrote by hand.\n---\n\n"
                f"Reply with {self.run_id}.\n"
            )
        assert not (self.home / ".ucode").exists(), "Seeding must not create ug state"
        return SeededUserState(
            claude_settings=claude_settings.read_text(),
            codex_keys=self.user_codex_keys(),
            mcp_registrations={
                agent: entries[USER_MCP] for agent, entries in self.registrations().items()
            },
            skill_files=self.skill_files(),
        )

    def user_codex_keys(self) -> dict:
        config = read_toml(self.home / ".codex/config.toml")
        return {key: config.get(key) for key in ("hide_agent_reasoning", "projects")}

    def assert_user_state_preserved(self, seeded: SeededUserState) -> None:
        assert (self.home / ".claude/settings.json").read_text() == seeded.claude_settings
        assert self.user_codex_keys() == seeded.codex_keys
        registrations = self.registrations()
        for agent, entry in seeded.mcp_registrations.items():
            assert registrations[agent].get(USER_MCP) == entry, f"{agent} user MCP changed"
            assert USER_MCP in self.mcp_list(agent), f"{agent} no longer discovers the user MCP"
        skills = self.skill_files()
        for path, digest in seeded.skill_files.items():
            assert skills.get(path) == digest, f"User skill changed: {path}"
        claude = shutil.which("claude", path=self.session.env["PATH"])
        health = self.session.run("mcp", "get", USER_MCP, binary=claude, timeout=120).stdout
        assert "Connected" in health, f"User MCP is not usable:\n{health}"

    def assert_managed_mcp_absent(self) -> None:
        for agent, entries in self.registrations().items():
            assert MANAGED_MCP_ENTRY not in entries, f"{agent} kept the removed managed MCP"
            assert MANAGED_MCP_ENTRY not in self.mcp_list(agent), f"{agent} still lists it"

    def test_cuj_repeated_config_mixed_sources_and_removal(self):
        """Scenario: seed user-owned prefs, MCP, and skill; publish and configure Phase A
        twice, then Phase B (Claude on MPS, Codex on UC discovery, headers and selectors
        changed), then Phase C (Claude disabled, Codex on a Luna-only static list).

        Expected: the repeat configure changes nothing; each phase's sources, headers,
        picker, default agent, and tracing take effect with no stale ug-managed values;
        the removed MCP stays gone across restarts; disabled Claude fails before
        inference; user-owned state is unchanged throughout. Headers are asserted in the
        agents' effective config, not at gateway ingress.
        """
        session = self.session
        seeded = self.seed_user_state()
        self.trace_table = resolve_trace_table(self.WORKSPACE_URL, session.env["DATABRICKS_BEARER"])
        self.warehouse_id = resolve_warehouse_id(
            self.WORKSPACE_URL, session.env["DATABRICKS_BEARER"]
        )

        # Phase A: the same config twice is idempotent.
        # TODO(AIGTWY-4881): add skills.names once UC skill bundles work on this workspace's
        # Default Storage catalog (Files API HTTP 501).
        self.publish("a", phase_a(self.run_id))
        self.configure(terminal=False)
        first = self.effective_settings()
        self.configure(terminal=False)
        second = self.effective_settings()
        session.record("phase-a-settings.json", {"first": first, "second": second})
        assert second == first, "A repeat configure changed effective settings"
        for agent, entries in second["registrations"].items():
            assert {MANAGED_MCP_ENTRY, USER_MCP} <= set(entries), f"{agent} dropped an MCP entry"
            serialized = [json.dumps(entry, sort_keys=True) for entry in entries.values()]
            assert len(serialized) == len(set(serialized)), f"{agent} registered an MCP twice"
        for agent in ("claude", "codex"):
            assert MANAGED_MCP_ENTRY in second[f"{agent}_mcp_list"], (
                f"{agent} lacks the managed MCP"
            )
        self.assert_served(self.claude_task(marker=None), "system.ai.claude-sonnet-4-6")
        assert self.codex_task(marker=None) == {CODEX_SOL}
        self.assert_user_state_preserved(seeded)

        # Phase B: mixed sources, header changes, and selector removal.
        config_b = phase_b(self.run_id)
        self.publish("b", config_b)
        self.configure(terminal=False)
        assert self.is_published(config_b), "Readback no longer matches config B"
        claude_settings = read_json(self.home / ".claude/ucode-settings.json")
        claude_env = self.claude_env()
        claude_headers = self.claude_headers()
        assert claude_headers.get("Databricks-Model-Provider-Service") == CLAUDE_PROVIDER
        assert not set(CLAUDE_CUSTOM_HEADER_NAMES) & set(claude_headers), claude_headers
        assert not {"availableModels", "enforceAvailableModels"} & set(claude_settings), (
            "Phase A's static Claude picker survived"
        )
        picker_options = json.dumps(claude_settings.get("modelPicker", {}))
        assert not [m for m in CLAUDE_STATIC_MODELS if m in picker_options], picker_options
        stale_defaults = {
            key: value
            for key, value in claude_env.items()
            if key.startswith("ANTHROPIC_DEFAULT_")
            and value.removesuffix("[1m]") in CLAUDE_STATIC_MODELS
        }
        assert not stale_defaults, f"Phase A's Claude family defaults survived: {stale_defaults}"
        assert "otelHeadersHelper" not in claude_settings
        assert not set(CLAUDE_TRACE_ENV_KEYS) & set(claude_env), claude_env
        codex_headers = self.codex_headers()
        assert codex_headers.get("X-UG-E2E-Run") == f"{self.run_id}-phase-b", codex_headers
        assert "X-UG-E2E-Agent" not in codex_headers, codex_headers
        assert codex_headers.get("Databricks-Model-Service-Parent-Schema") == CODEX_MODEL_LOCATION
        assert "model_catalog_json" not in read_toml(self.home / ".codex/config.toml")
        self.assert_managed_mcp_absent()
        self.assert_user_state_preserved(seeded)

        claude_marker = f"{self.run_id}-claude-b"
        codex_marker = f"{self.run_id}-codex-b"
        self.assert_served(self.claude_task(marker=claude_marker), CLAUDE_PROVIDER_MODEL)
        assert self.codex_task(marker=codex_marker) == {CODEX_UC_MODEL}
        # Last in Phase B: this terminal launch also writes the OS-managed Codex file.
        assert self.bare_ug_task("phase-b-bare-ug") == {CODEX_UC_MODEL}
        os_provider = read_toml(CODEX_OS_MANAGED_CONFIG)["model_providers"]["Databricks"]
        assert os_provider["http_headers"]["X-UG-E2E-Run"] == f"{self.run_id}-phase-b"
        # Codex's span proves the destination is up before Claude's absence counts.
        self.wait_for_trace(codex_marker)
        assert self.trace_count(claude_marker, resource=True) == 0, "Claude traced with tracing off"
        # The restarts above must not bring back anything Phase B removed.
        self.assert_managed_mcp_absent()

        # Phase C: Claude disabled, Codex back to a static list without headers or tracing.
        self.publish("c", phase_c())
        # Phase B's terminal launch made ug own the OS-managed file; only a terminal rewrites it.
        self.configure(terminal=True)
        stale_headers = {"X-UG-E2E-Run", "Databricks-Model-Service-Parent-Schema"}
        assert (self.home / ".codex/ucode.config.toml").is_file()
        assert not stale_headers & set(self.codex_headers()), self.codex_headers()
        assert CODEX_OS_MANAGED_CONFIG.is_file()
        os_provider = read_toml(CODEX_OS_MANAGED_CONFIG)["model_providers"]["Databricks"]
        assert not stale_headers & set(os_provider.get("http_headers", {})), os_provider
        picker = session.codex_model_ids(["app-server", "--listen", "stdio://"], "phase-c-models")
        assert picker == [CODEX_LUNA], picker
        assert self.bare_ug_task("phase-c-bare-ug") == {CODEX_LUNA}

        claude_sessions = set(agent_sessions(session, "claude"))
        rejected = session.run("claude", "--", "-p", "unused", ok=False, timeout=120)
        assert rejected.returncode != 0
        assert "doesn't enable Claude Code" in rejected.stdout + rejected.stderr, rejected
        assert set(agent_sessions(session, "claude")) == claude_sessions, "Claude ran inference"

        codex_marker_c = f"{self.run_id}-codex-c"
        assert self.codex_task(marker=codex_marker_c) == {CODEX_LUNA}
        # Absence can't be polled; wait out the ingestion window before counting.
        time.sleep(TRACE_INGESTION_SECONDS)
        # Phase B's span is still queryable, so a zero below is not a query outage.
        assert self.trace_count(codex_marker, resource=False) > 0, "Trace query is unhealthy"
        assert self.trace_count(codex_marker_c, resource=False) == 0, "Codex traced with it off"
        self.assert_user_state_preserved(seeded)
