"""Agent learning loop, modeled on Microsoft's Agent Lightning
(github.com/microsoft/agent-lightning): every agent run becomes a *rollout*
(prompt → actions → outcome) with a *reward*, and those rewarded traces drive
optimization.

How the loop closes here:
- Every /chat run is recorded as a trace with a heuristic reward (did SQL run,
  did a chart render, were there errors along the way).
- User 👍/👎 feedback overwrites the heuristic — explicit reward beats guessed.
- Recent failures are injected into the system prompt ("known pitfalls"), so
  the agent learns from bugs immediately, with no training run at all.
- scripts/train_apo.py distills low-reward traces into prompts/system_learned.txt
  (Agent Lightning's APO idea: optimize the prompt, not the weights) — the
  right lever for API models like Claude/GPT, whose weights we can't touch.
- Full RL (VERL/GRPO) needs a self-hosted open-weight model on GPUs; the traces
  exported by export_rollouts() are exactly what that pipeline consumes, so
  nothing here has to change to graduate to it.
"""
import json
import os

from . import db


def agl_available():
    """Version string when the agentlightning package is importable, else None."""
    try:
        import agentlightning
        return getattr(agentlightning, "__version__", "installed")
    except Exception:
        return None


def heuristic_reward(result):
    """Score a run 0..1 from observable outcomes. User feedback replaces this.

    Fallback-mode runs return None — deterministic previews aren't agent
    behavior, and training on them would teach nothing.
    """
    if result.get("mode") != "agent":
        return None
    text = result.get("text") or ""
    if text.startswith("(Agent error"):
        return 0.0
    r = 0.35  # answered at all
    if result.get("sql"):
        r += 0.25  # grounded in a real query
    if result.get("rows"):
        r += 0.15  # the query produced data
    chart = result.get("chart") or {}
    if chart.get("type") and chart.get("type") != "table":
        r += 0.15  # visualized the answer
    if len(result.get("panels") or []) > 1:
        r += 0.10  # multi-view answer
    r -= 0.10 * min(len(result.get("errors") or []), 2)  # stumbles along the way
    return round(max(0.0, min(1.0, r)), 2)


def record_chat_trace(user, conversation_id, prompt, result, duration_ms):
    """Persist one rollout. Returns the trace id (also stored in the message,
    so the UI's 👍/👎 can target it later)."""
    errors = result.get("errors") or []
    chart = result.get("chart") or {}
    try:
        return db.add_trace(
            user,
            conversation_id=conversation_id,
            prompt=prompt[:1000],
            model=result.get("model"),
            mode=result.get("mode"),
            source=result.get("source"),
            table=result.get("table"),
            sql=result.get("sql"),
            ok=not (result.get("text") or "").startswith("(Agent error"),
            error=errors[0][:500] if errors else None,
            row_count=len(result.get("rows") or []),
            chart_type=chart.get("type"),
            panel_count=len(result.get("panels") or []),
            duration_ms=duration_ms,
            reward=heuristic_reward(result),
            reward_source="heuristic",
            meta={"errors": errors[:5]},
        )
    except Exception:
        return None  # learning must never break answering


def export_rollouts(path, limit=5000):
    """Write traces as JSONL in the shape RL/APO training jobs consume."""
    n = 0
    with open(path, "w") as f:
        for t in db.list_traces(limit=limit):
            if t.get("reward") is None:
                continue
            f.write(json.dumps({
                "prompt": t["prompt"],
                "response": {"sql": t["sql"], "chart_type": t["chart_type"]},
                "reward": t["reward"],
                "metadata": {
                    "model": t["model"], "source": t["source"], "table": t["tbl"],
                    "error": t["error"], "reward_source": t["reward_source"],
                    "duration_ms": t["duration_ms"],
                },
            }, default=str) + "\n")
            n += 1
    return n
