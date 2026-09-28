import os
import tempfile
import json

import pytest
from fastapi.testclient import TestClient

# Set before canary_api is imported so the bus writes to a throwaway directory.
os.environ["CANARY_RUNS_DIR"] = tempfile.mkdtemp(prefix="canary_runs_")
os.environ["CANARY_ALLOW_LIVE"] = "true"
# The default suite always runs on the file backend, whatever the shell exports.
os.environ.pop("DATABASE_URL", None)
# Developer shells may export demo settings; tests must not inherit them (a fake live call would then receive
# arguments it does not accept, or a run would pick up the wrong mode). Tests that need one set it themselves.
# Popped here too because the session client starts the app before any function fixture runs.
ISOLATED_ENV = ("CANARY_LLM_THINKING", "CANARY_CHALLENGER_THINKING", "CANARY_STRUCTURED_OUTPUT", "CANARY_SIM_MODE",
                "CANARY_AGENT_CONTEXT_HOPS")
for _name in ISOLATED_ENV:
    os.environ.pop(_name, None)

from canary_api.app import app  # noqa: E402
from canary_api import runtime, storage  # noqa: E402
from agent_orchestration import AgentLLM, DecisionIntake  # noqa: E402
from agent_orchestration.llm import LiveReply  # noqa: E402
from contracts_py.agents import ChallengerOutput  # noqa: E402
from company_twin import load_company_twin  # noqa: E402
from real_data import sample_brief  # noqa: E402

# Production loads the active twin from Postgres. Tests seed the isolated SQLite
# backend explicitly so API behavior is exercised without a production database.
storage.current().save_twin(load_company_twin(), active=True)


def test_live_call(model_id, messages, output_model, *, timeout, temperature, structured_output="auto"):
    if output_model is ChallengerOutput:
        return LiveReply({"confidence": 0.5})
    return LiveReply({
        "act_now_view": {"summary": "Test live assessment."},
        "inaction_view": {"summary": "Test live baseline."},
        "confidence": 0.5,
    })


def build_test_llm(settings):
    """Explicit test double for API tests that do not opt into paid provider calls."""
    return AgentLLM(
        settings.model_copy(update={"model_id_strong": "test-provider", "model_id_fast": "test-provider"}),
        live_call=test_live_call,
    )


def test_intake_call(model_id, messages, output_model, *, timeout, temperature, structured_output="auto"):
    payload = json.loads(messages[1]["content"])
    departments = [item["id"] for item in payload["entity_catalog"] if item["type"] == "department"]
    return LiveReply({
        "decision_type": "mixed",
        "title": payload["user_request"][:120],
        "goal": {
            "metric": "net_value_usd",
            "target": 0,
            "unit": "usd",
            "basis": "net",
            "direction": "at_least",
        },
        "candidate_interventions": [
            {
                "type": "assess_change",
                "target_entity_id": department_id,
                "rationale": "Assess the requested change without assuming a quantitative effect.",
            }
            for department_id in departments
        ],
        "constraints": [],
        "assumptions": ["Unquantified effects remain unknown until supported by supplied values."],
        "warnings": [],
    })


def build_test_intake(settings):
    return DecisionIntake(
        settings.model_copy(update={"model_id_strong": "test-provider", "model_id_fast": "test-provider"}),
        live_call=test_intake_call,
    )


@pytest.fixture(autouse=True)
def agent_provider(request, monkeypatch):
    """Keep ordinary tests offline without allowing them to masquerade as live-provider checks."""
    if request.node.get_closest_marker("live_agents") is None:
        monkeypatch.setattr(runtime, "build_llm", build_test_llm)
        monkeypatch.setattr(runtime, "build_intake", build_test_intake)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def brief_json() -> dict:
    return sample_brief().model_dump(mode="json")


@pytest.fixture(autouse=True)
def isolated_demo_env(monkeypatch):
    for name in ISOLATED_ENV:
        monkeypatch.delenv(name, raising=False)
