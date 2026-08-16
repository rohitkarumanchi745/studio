#!/usr/bin/env python3
"""Online BitNet trainer — the concurrent consumer half of the loop.

Runs on a GPU host ALONGSIDE Studio (which keeps serving). It continuously:

    1. pulls new reward-labeled rollouts from Studio  (GET /training/rollouts)
    2. trains LoRA adapters on them
         · a GLOBAL tool-calling adapter  (all users' tool-call decisions)
         · a PER-USER style adapter        (each user's own rollouts)
    3. publishes the new adapter versions back         (POST /training/adapters)

Studio's serving hot-swaps to the newest adapter on the next request, so
training and serving run simultaneously. Order of methods by stability — start
with SFT (behaviour-clone the successful tool calls), add DPO (prefer success
over failure), and only then GRPO. This file has the full loop, data shaping,
and Studio handshake; the actual LoRA fine-tune step is marked TODO because it
needs a GPU + the BitNet base (transformers + peft) which don't run in Studio.

Usage:
    STUDIO_URL=https://studio.example.com \
    STUDIO_TOKEN=<admin bearer> \
    BITNET_BASE=microsoft/bitnet-b1.58-2B \
    python train_online.py --min-batch 64 --user-min 32 --poll 30
"""
import argparse
import os
import time
from collections import defaultdict

import requests   # trainer host dependency, not Studio's

STUDIO_URL = os.environ.get("STUDIO_URL", "http://localhost:8000")
TOKEN = os.environ.get("STUDIO_TOKEN", "")
BASE_MODEL = os.environ.get("BITNET_BASE", "microsoft/bitnet-b1.58-2B")
ADAPTER_DIR = os.environ.get("ADAPTER_DIR", "./adapters")
H = {"Authorization": f"Bearer {TOKEN}"}


def pull(since):
    r = requests.get(f"{STUDIO_URL}/api/training/rollouts",
                     params={"since": since, "limit": 1000}, headers=H, timeout=30)
    r.raise_for_status()
    return r.json()


def publish(scope, kind, uri, metrics):
    r = requests.post(f"{STUDIO_URL}/api/training/adapters", headers=H, timeout=30,
                      json={"scope": scope, "kind": kind, "uri": uri,
                            "base_model": BASE_MODEL, "metrics": metrics})
    r.raise_for_status()
    return r.json()


# ── The LoRA training step (GPU host) ────────────────────────────────────

def train_tool_call_lora(rollouts):
    """SFT/DPO a GLOBAL tool-calling LoRA on the successful tool-call rollouts.

    Each rollout carries the prompt and the action the decision-maker took
    (action.sql = the run_sql tool call). Behaviour-clone reward>=0.7 calls;
    build preference pairs (success > failure) on the same prompt for DPO.
    """
    good = [r for r in rollouts if (r.get("reward") or 0) >= 0.7 and r["action"].get("sql")]
    # sft_examples: [{"prompt": r["prompt"], "tool_call": {"name":"run_sql","args":{"sql": ...}}}, ...]
    # dpo_pairs:    same prompt, chosen=success action, rejected=failure action
    # TODO(GPU): load BASE_MODEL as BitLinear, attach a fresh LoRA (peft), run
    #   SFT then DPO with grammar-constrained tool-call decoding, save to disk.
    uri = f"{ADAPTER_DIR}/tool_call"
    metrics = {"method": "sft+dpo", "n_rollouts": len(rollouts), "n_good": len(good)}
    return uri, metrics


def train_user_lora(user_id, rollouts):
    """Train a small PER-USER style LoRA on just this user's rollouts — bakes
    their preferences (chart types, regions, phrasing) into weights, the
    weight-level upgrade of Studio's `remember`/memory notes."""
    uri = f"{ADAPTER_DIR}/user/{user_id}"
    metrics = {"method": "sft", "n_rollouts": len(rollouts)}
    return uri, metrics


# ── The simultaneous loop ────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=int, default=30, help="seconds between pulls")
    ap.add_argument("--min-batch", type=int, default=64, help="rollouts before a tool-call train step")
    ap.add_argument("--user-min", type=int, default=32, help="rollouts before a per-user train step")
    args = ap.parse_args()

    cursor = 0.0
    buffer = []
    per_user = defaultdict(list)
    print(f"[online-trainer] streaming from {STUDIO_URL}, base={BASE_MODEL}")

    while True:
        try:
            data = pull(cursor)
        except Exception as e:
            print(f"[online-trainer] pull failed: {e}; retrying")
            time.sleep(args.poll)
            continue

        new = data["rollouts"]
        cursor = data["cursor"]
        for r in new:
            buffer.append(r)
            if r.get("user_id"):
                per_user[r["user_id"]].append(r)
        if new:
            print(f"[online-trainer] +{len(new)} rollouts (buffer {len(buffer)})")

        # Global tool-calling adapter — trains on everyone's tool-call decisions.
        if len(buffer) >= args.min_batch:
            uri, metrics = train_tool_call_lora(buffer)
            res = publish("global", "tool_call", uri, metrics)
            print(f"[online-trainer] published tool_call v{res['version']} ({metrics})")
            buffer = []

        # Per-user style adapters — one per user with enough of their own data.
        for uid, rs in list(per_user.items()):
            if len(rs) >= args.user_min:
                uri, metrics = train_user_lora(uid, rs)
                res = publish(uid, "user_style", uri, metrics)
                print(f"[online-trainer] published user_style[{uid[:8]}] v{res['version']}")
                per_user[uid] = []

        time.sleep(args.poll)


if __name__ == "__main__":
    main()
