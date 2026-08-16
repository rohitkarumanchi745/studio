#!/usr/bin/env python3
"""Online BitNet trainer — the concurrent consumer half of the loop.

Runs on a CPU WORKER alongside Studio (which keeps serving). BitNet is a 1-bit
model: its ternary base is CPU-efficient (bitnet.cpp / llama.cpp), and LoRA
adapters are small full-precision matrices — so this trains on CPU. A GPU only
speeds it up; `--device auto` uses CUDA when present, CPU otherwise. It loops:

    1. pulls new reward-labeled rollouts from Studio  (GET /training/rollouts)
    2. trains LoRA adapters on them
         · a GLOBAL tool-calling adapter  (all users' tool-call decisions)
         · a PER-USER style adapter        (each user's own rollouts)
    3. publishes the new adapter versions back         (POST /training/adapters)

Studio hot-swaps to the newest adapter on the next request, so training and
serving run simultaneously. On CPU the cadence is coarser (periodic batches, not
instant). Method order by stability: SFT (behaviour-clone the successful tool
calls) → DPO (prefer success over failure) → GRPO. The loop, data shaping, and
Studio handshake are real; the LoRA step runs for real when the ML deps
(requirements-trainer.txt) and the BitNet base are present, and no-ops with a
clear message otherwise so the handshake is still exercisable in dev.

Deploy: run as a separate CPU service (see Dockerfile.trainer) — it needs only
STUDIO_URL + an admin token, no GPU.

Usage:
    STUDIO_URL=https://studio.example.com STUDIO_TOKEN=<admin bearer> \
    BITNET_BASE=microsoft/bitnet-b1.58-2B \
    python train_online.py --device auto --min-batch 64 --user-min 32 --poll 60
"""
import argparse
import os
import time
from collections import defaultdict

import requests   # trainer worker dependency, not Studio's

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


# ── The LoRA training step (CPU by default; GPU optional) ────────────────

DEVICE = "cpu"   # set from --device in main()


def _sft_example(r):
    """One rollout → a supervised tool-call example: prompt ⇒ the run_sql call."""
    return {"prompt": r["prompt"] or "",
            "target": '{"name":"run_sql","arguments":{"sql":"%s"}}' % (r["action"].get("sql") or "")}


def _train_lora(examples, out_dir, cfg):
    """Real LoRA SFT — runs on CPU. Freezes the ternary BitNet base and trains
    only the small full-precision LoRA, which is why CPU is enough. No-ops with a
    message when the ML deps or base model aren't installed, so the loop still
    exercises the publish handshake in dev."""
    if not examples:
        return {"trained": False, "n": 0, "note": "no examples"}
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model
        from transformers import (AutoModelForCausalLM, AutoTokenizer, Trainer,
                                   TrainingArguments)
    except Exception as e:
        print(f"[online-trainer] ML deps missing ({e}) — install requirements-trainer.txt; publishing stub")
        return {"trained": False, "n": len(examples), "note": "deps missing"}

    tok = AutoTokenizer.from_pretrained(BASE_MODEL)
    tok.pad_token = tok.pad_token or tok.eos_token
    # BitNet loads as a normal causal LM; the ternary BitLinear base stays frozen
    # — get_peft_model attaches the trainable full-precision LoRA on top.
    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL, torch_dtype=torch.float32).to(DEVICE)
    model = get_peft_model(model, LoraConfig(
        r=8, lora_alpha=16, lora_dropout=0.05, task_type="CAUSAL_LM",
        # module names are architecture-specific — adjust for the BitNet build.
        target_modules=cfg.get("target_modules", ["q_proj", "v_proj"])))

    def tokenize(b):
        text = [f"{p}\n{t}{tok.eos_token}" for p, t in zip(b["prompt"], b["target"])]
        enc = tok(text, truncation=True, max_length=512, padding="max_length")
        enc["labels"] = enc["input_ids"].copy()
        return enc

    ds = Dataset.from_list(examples).map(tokenize, batched=True,
                                         remove_columns=["prompt", "target"])
    args = TrainingArguments(
        output_dir=out_dir, per_device_train_batch_size=cfg.get("batch", 4),
        num_train_epochs=cfg.get("epochs", 1), learning_rate=2e-4,
        no_cuda=(DEVICE == "cpu"), logging_steps=10, report_to=[], save_strategy="no")
    Trainer(model=model, args=args, train_dataset=ds).train()
    model.save_pretrained(out_dir)
    return {"trained": True, "n": len(examples), "device": DEVICE}


def train_tool_call_lora(rollouts):
    """GLOBAL tool-calling LoRA — behaviour-clone the successful tool calls
    (reward ≥ 0.7). DPO on success-vs-failure pairs is the natural next pass."""
    good = [_sft_example(r) for r in rollouts
            if (r.get("reward") or 0) >= 0.7 and r["action"].get("sql")]
    uri = f"{ADAPTER_DIR}/tool_call"
    metrics = _train_lora(good, uri, {"epochs": 1})
    metrics.update(method="sft", n_rollouts=len(rollouts), n_good=len(good))
    return uri, metrics


def train_user_lora(user_id, rollouts):
    """PER-USER style LoRA on just this user's rollouts — bakes their preferences
    (chart types, regions, phrasing) into weights: the weight-level upgrade of
    Studio's `remember` / memory notes. Small data → fast even on CPU."""
    good = [_sft_example(r) for r in rollouts if r["action"].get("sql")]
    uri = f"{ADAPTER_DIR}/user/{user_id}"
    metrics = _train_lora(good, uri, {"epochs": 2})
    metrics.update(method="sft", n_rollouts=len(rollouts))
    return uri, metrics


# ── The simultaneous loop ────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=int, default=60, help="seconds between pulls")
    ap.add_argument("--min-batch", type=int, default=64, help="rollouts before a tool-call train step")
    ap.add_argument("--user-min", type=int, default=32, help="rollouts before a per-user train step")
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto",
                    help="CPU is enough for BitNet; auto uses CUDA only if present")
    args = ap.parse_args()

    global DEVICE
    if args.device == "auto":
        try:
            import torch
            DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            DEVICE = "cpu"
    else:
        DEVICE = args.device

    cursor = 0.0
    buffer = []
    per_user = defaultdict(list)
    print(f"[online-trainer] streaming from {STUDIO_URL}, base={BASE_MODEL}, device={DEVICE}")

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
