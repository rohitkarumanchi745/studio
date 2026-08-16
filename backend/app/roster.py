"""Named agents — who's who, and which one answered.

Studio runs a small crew. Each connected data source gets its own WORKER agent,
briefed by that source's skill file; a cross-source question adds the AGGREGATOR
that synthesizes their answers; the ORCHESTRATOR is the fan-out coordinator.
This module gives every agent a stable NAME so each run can report exactly which
agents were called (surfaced on the answer), and the learning store
(agent_traces.meta.agents) can attribute each rollout to the agents behind it.
"""
from . import rbac
from .connectors import all_sources, get_connector


def name_for(source):
    """Stable display name for a source's worker agent ("Snowflake agent")."""
    if source in (None, "*"):
        return "Orchestrator"
    return f"{source[:1].upper()}{source[1:]} agent"


def worker(source):
    return {"name": name_for(source), "source": source, "role": "worker"}


AGGREGATOR = {"name": "Aggregator", "source": "*", "role": "aggregator"}
ORCHESTRATOR = {"name": "Orchestrator", "source": "*", "role": "orchestrator"}

# The staged pipeline crew (flow.py): each turns one typed JSON artifact into
# the next, and each run is recorded as its own Agent Lightning trace.
STAGE_AGENTS = [
    {"name": "Pipeline planner", "stage": "plan", "role": "stage",
     "produces": "PipelineSpec"},
    {"name": "Code generator", "stage": "codegen", "role": "stage",
     "produces": "GeneratedArtifact"},
    {"name": "Validator", "stage": "validate", "role": "stage",
     "produces": "ValidationResult"},
    {"name": "Approval agent", "stage": "approve", "role": "stage",
     "produces": "DeploymentRequest"},
    {"name": "Deployment executor", "stage": "execute", "role": "stage",
     "produces": "ExecutionResult"},
]


def stage(name):
    """The named stage agent for a flow stage (plan/codegen/validate/…)."""
    return next((a for a in STAGE_AGENTS if a["stage"] == name),
                {"name": name, "stage": name, "role": "stage"})


def executor_name(target):
    """The executor agent is named for where it deploys ("Databricks executor")."""
    if not target or target == "*":
        return "Deployment executor"
    return f"{target[:1].upper()}{target[1:]} executor"


def roster(user):
    """The named worker agents this user's role can call, each with the tables
    it's briefed on. Lightweight (no schema fetch) so it's cheap to show."""
    role = user["role"]
    out = []
    for meta in all_sources():
        name = meta["name"]
        accessible = meta["configured"] and name in rbac.allowed_sources(role)
        entry = {**worker(name), "configured": meta["configured"], "accessible": accessible}
        if accessible:
            try:
                conn = get_connector(name)
                tbls = rbac.allowed_tables(role, name, conn.list_tables())
                entry["tables"] = tbls[:20]
                entry["table_count"] = len(tbls)
            except Exception:
                entry["tables"], entry["table_count"] = [], 0
        out.append(entry)
    return out


def summary(user):
    """Roster + whether a question would fan out (>1 accessible source → the
    Aggregator joins). This is the crew, and who gets called when."""
    agents = roster(user)
    accessible = [a for a in agents if a.get("accessible")]
    return {
        "agents": agents,
        "orchestrator": ORCHESTRATOR,
        "aggregator": AGGREGATOR,
        "pipeline_crew": STAGE_AGENTS,
        "multi_source": len(accessible) > 1,
        "accessible_count": len(accessible),
    }
