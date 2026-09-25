"""Offline learning job — Agent Lightning's APO idea applied to Studio.

Reads rewarded traces from studio.db, clusters the failures, and drafts a
learned-rules proposal from them (app/learned_rules.py) — the "Learned
guidance" block every agent run receives once an admin approves it. Prompt
optimization is the right training lever for API models (Claude/GPT): their
weights can't be fine-tuned by us, but their instructions can be evolved from
evidence. The worker drafts proposals on its own schedule; this script is the
on-demand path.

Run from backend/:            .venv/bin/python scripts/train_apo.py
Draft and activate at once:   .venv/bin/python scripts/train_apo.py --approve
  (you are the reviewer: read the printed rules before you pass --approve)
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

from app import db, learned_rules, lightning  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", help="write rollouts JSONL for external RL training")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--approve", action="store_true",
                    help="activate the drafted rules immediately instead of leaving "
                         "them for an admin to approve in the app")
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

    if not db.list_traces(limit=1, max_reward=0.4):
        print("no low-reward traces yet — nothing to learn from. "
              "Use the app, give 👍/👎, then rerun.")
        return

    learned_rules.init_tables()
    proposal, reason = learned_rules.draft(force=True)
    if proposal is None:
        print(f"\nno rules drafted: {reason}")
        return
    print(f"\ndrafted rule set {proposal['id']} from {proposal['evidence_count']} "
          f"low-reward runs:\n{proposal['rules']}")
    if args.approve:
        learned_rules.approve(proposal["id"], "train_apo.py")
        print("\nactivated — every future agent run now receives these rules.")
    else:
        print("\nwaiting for an admin to approve it (Learned rules page), or rerun "
              "with --approve.")


if __name__ == "__main__":
    main()
