"""Offline learning job — Agent Lightning's APO idea applied to Studio.

Reads rewarded traces from studio.db, clusters the failures, and distills
them into prompts/system_learned.txt — the "Learned guidance" block every
future agent run receives. Prompt optimization is the right training lever
for API models (Claude/GPT): their weights can't be fine-tuned by us, but
their instructions can be evolved from evidence.

Run from backend/:            .venv/bin/python scripts/train_apo.py
Export rollouts for real RL:  .venv/bin/python scripts/train_apo.py --export rollouts.jsonl
  (feed the JSONL to an Agent Lightning VERL/GRPO pipeline against a
  self-hosted open-weight model — that part needs GPUs, not this laptop.)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from app import db, lightning  # noqa: E402
from app.agent import llm_available, llm_spec  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "system_learned.txt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", help="write rollouts JSONL for external RL training")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    if args.export:
        n = lightning.export_rollouts(args.export, limit=args.limit)
        print(f"exported {n} rewarded rollouts -> {args.export}")
        return

    stats = db.trace_stats()
    print(f"traces: {stats['total']}  avg reward: {stats['avg_reward'] and round(stats['avg_reward'], 2)}")
    print(f"agentlightning package: {lightning.agl_available() or 'not installed (optional)'}")
    for c in stats["failure_clusters"]:
        print(f"  {c['n']:>3}x  {c['cluster']}")

    bad = db.list_traces(limit=args.limit, max_reward=0.4)
    if not bad:
        print("no low-reward traces yet — nothing to learn from. "
              "Use the app, give 👍/👎, then rerun.")
        return

    evidence = "\n".join(
        f"- prompt: {t['prompt'][:150]!r}\n  sql: {(t['sql'] or 'none')[:200]}\n"
        f"  error: {(t['error'] or 'none')[:200]}  reward: {t['reward']} ({t['reward_source']})"
        for t in bad[:40]
    )

    if not llm_available():
        print(f"\n{len(bad)} low-reward traces collected, but no LLM key is set, so the "
              "distillation step can't run. Add ANTHROPIC_API_KEY/OPENAI_API_KEY and rerun.")
        return

    from langchain.chat_models import init_chat_model
    llm = init_chat_model(llm_spec())
    out = llm.invoke(
        "You are optimizing the system prompt of a SQL analytics agent. Below are its "
        "recent low-reward runs (bad SQL, errors, user thumbs-down). Distill AT MOST 6 "
        "short imperative rules that would prevent these specific failures. Output only "
        "the rules as '- ' bullet lines, no preamble.\n\nFailures:\n" + evidence
    )
    rules = out.content if hasattr(out, "content") else str(out)

    os.makedirs(os.path.dirname(PROMPT_PATH), exist_ok=True)
    if os.path.exists(PROMPT_PATH):
        os.replace(PROMPT_PATH, PROMPT_PATH + ".bak")
    with open(PROMPT_PATH, "w") as f:
        f.write(rules.strip() + "\n")
    print(f"\nwrote {PROMPT_PATH} — every future agent run now receives these rules:\n{rules}")


if __name__ == "__main__":
    main()
