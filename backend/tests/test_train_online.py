"""scripts/train_online.py — the parts that decide whether a round runs AT ALL
on someone else's machine.

The trainer is the GPU half of the BitNet loop (serving BitNet is the CPU half:
stock vLLM cannot load it, bitnet.cpp on CPU is the supported runtime). The
target box is a Windows + NVIDIA laptop, and this suite runs on an Apple M1 with
no CUDA and no Windows — so what is pinned here is everything that can be
decided WITHOUT that hardware:

  - the published adapter uri keeps the SERVER's separator style, because the
    trainer's os.sep is not the server's (a Windows trainer publishing
    '/adapters' + os.path.join would emit '/adapters\\tool_call-1');
  - dtype/device resolution per card generation — bf16 from Ampere on, fp16 on
    the pre-Ampere cards that only emulate it, fp32 only on plain CPU;
  - the VRAM knobs are configurable at all (max_length, batch, grad-accum,
    gradient checkpointing) and default the way an 8 GB card needs;
  - an out-of-memory failure becomes instructions naming the exact env vars,
    not a CUDA traceback;
  - the grad-accum arithmetic that makes the progress bar look frozen is stated
    before training starts;
  - the 4.8 GB download announces itself before it blocks.

torch is faked (sys.modules injection) wherever a decision needs it: the point
is the branch, not the tensor.

Run from the backend directory:
    python -m pytest tests/test_train_online.py -q
"""
import importlib.util
import os
import sys

import pytest

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts", "train_online.py")


def _load(env=None):
    """Exec the script fresh, optionally under `env`. Its knobs are module-level
    constants read at import (as the rest of the file's config is), so a config
    test is a re-import, not a monkeypatch."""
    saved = dict(os.environ)
    try:
        for k, v in (env or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        spec = importlib.util.spec_from_file_location("train_online_under_test", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


T = _load()


# ── the published uri is the SERVER's path, in the SERVER's separators ───

@pytest.mark.parametrize("base,expected", [
    ("/adapters", "/adapters/tool_call-1"),
    ("/adapters/", "/adapters/tool_call-1"),
    ("/mnt/c/studio/adapters", "/mnt/c/studio/adapters/tool_call-1"),
    ("C:\\studio\\adapters", "C:\\studio\\adapters\\tool_call-1"),
    ("C:\\studio\\adapters\\", "C:\\studio\\adapters\\tool_call-1"),
    ("C:/studio/adapters", "C:/studio/adapters/tool_call-1"),
    ("\\\\nas\\share\\adapters", "\\\\nas\\share\\adapters\\tool_call-1"),
    ("http://serving:9000/adapters", "http://serving:9000/adapters/tool_call-1"),
])
def test_uri_join_follows_the_base_not_the_local_os(base, expected):
    """os.path.join would splice the TRAINER's separator into the SERVER's path.
    A Windows trainer publishing to a Linux server must still emit POSIX."""
    assert T._uri_join(base, "tool_call-1") == expected


def test_uri_join_of_a_windows_output_dir_onto_a_posix_base_stays_posix():
    """The real call site: a Windows adapter_dir, a container base uri. This is
    the bug os.path.join would introduce ('/adapters\\tool_call-1')."""
    adapter_dir = "C:\\studio\\adapters\\tool_call-1723890000"
    uri = T._uri_join("/adapters", T._uri_basename(adapter_dir))
    assert uri == "/adapters/tool_call-1723890000"
    assert "\\" not in uri


@pytest.mark.parametrize("uri,name", [
    ("/adapters/tool_call-1", "tool_call-1"),
    ("/adapters/tool_call-1/", "tool_call-1"),
    ("C:\\studio\\adapters\\tool_call-1", "tool_call-1"),
    ("C:\\studio\\adapters\\tool_call-1\\", "tool_call-1"),
    ("tool_call-1", "tool_call-1"),
])
def test_uri_basename_splits_on_either_separator(uri, name):
    """push_to_serving derives vLLM's lora_name from this; os.path.basename on
    Linux would hand back the whole 'C:\\...' string as one segment."""
    assert T._uri_basename(uri) == name


def test_windows_uri_gets_a_wsl_translation_hint():
    hint = T._cross_os_uri_hint("C:\\studio\\adapters\\tool_call-1")
    assert "/mnt/c/studio/adapters/tool_call-1" in hint
    assert "STUDIO_TRAIN_ADAPTER_BASE_URI" in hint


def test_posix_uri_needs_no_hint():
    assert T._cross_os_uri_hint("/adapters/tool_call-1") is None
    assert T._cross_os_uri_hint("http://serving:9000/adapters/x") is None


# ── device + dtype per card generation ───────────────────────────────────

class _DType:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return "torch." + self.name

    __repr__ = __str__


class _FakeTorch:
    """Just enough torch for _device_and_dtype's branches."""

    def __init__(self, cuda=False, bf16=False, mps=False):
        self.bfloat16, self.float16, self.float32 = (
            _DType("bfloat16"), _DType("float16"), _DType("float32"))
        outer = self

        class _Cuda:
            @staticmethod
            def is_available():
                return cuda

            @staticmethod
            def is_bf16_supported():
                return bf16

        class _Mps:
            @staticmethod
            def is_available():
                return mps

        self.cuda = _Cuda
        self.backends = type("B", (), {"mps": _Mps})
        outer  # noqa: B018 - keep the closure explicit


@pytest.fixture
def fake_torch(monkeypatch):
    def install(**kw):
        t = _FakeTorch(**kw)
        monkeypatch.setitem(sys.modules, "torch", t)
        return t
    return install


def test_ampere_and_newer_get_bf16(fake_torch, monkeypatch):
    monkeypatch.delenv("STUDIO_TRAIN_DEVICE", raising=False)
    monkeypatch.delenv("STUDIO_TRAIN_DTYPE", raising=False)
    fake_torch(cuda=True, bf16=True)
    dev, dtype = T._device_and_dtype()
    assert (dev, str(dtype)) == ("cuda", "torch.bfloat16")


def test_pre_ampere_cards_fall_back_to_fp16_not_emulated_bf16(fake_torch, monkeypatch):
    """A GTX 16xx / RTX 20xx reports bf16 'available' but emulates it — slower
    than fp16 and prone to silent underflow."""
    monkeypatch.delenv("STUDIO_TRAIN_DEVICE", raising=False)
    monkeypatch.delenv("STUDIO_TRAIN_DTYPE", raising=False)
    fake_torch(cuda=True, bf16=False)
    dev, dtype = T._device_and_dtype()
    assert (dev, str(dtype)) == ("cuda", "torch.float16")


def test_apple_silicon_uses_mps_bf16(fake_torch, monkeypatch):
    monkeypatch.delenv("STUDIO_TRAIN_DEVICE", raising=False)
    monkeypatch.delenv("STUDIO_TRAIN_DTYPE", raising=False)
    fake_torch(cuda=False, mps=True)
    dev, dtype = T._device_and_dtype()
    assert (dev, str(dtype)) == ("mps", "torch.bfloat16")


def test_plain_cpu_stays_fp32(fake_torch, monkeypatch):
    monkeypatch.delenv("STUDIO_TRAIN_DEVICE", raising=False)
    monkeypatch.delenv("STUDIO_TRAIN_DTYPE", raising=False)
    fake_torch(cuda=False, mps=False)
    dev, dtype = T._device_and_dtype()
    assert (dev, str(dtype)) == ("cpu", "torch.float32")


def test_env_overrides_beat_the_heuristic(fake_torch, monkeypatch):
    fake_torch(cuda=True, bf16=True)
    monkeypatch.setenv("STUDIO_TRAIN_DEVICE", "cpu")
    monkeypatch.setenv("STUDIO_TRAIN_DTYPE", "fp16")
    dev, dtype = T._device_and_dtype()
    assert (dev, str(dtype)) == ("cpu", "torch.float16")


# ── the VRAM knobs are knobs ─────────────────────────────────────────────

def test_max_length_and_derived_prompt_budget_are_configurable():
    m = _load({"STUDIO_TRAIN_MAX_LENGTH": "512", "STUDIO_TRAIN_MAX_PROMPT_LENGTH": None})
    assert m.MAX_LENGTH == 512
    assert m.MAX_PROMPT_LENGTH == 256      # half, unless set explicitly


def test_prompt_budget_can_be_set_independently():
    m = _load({"STUDIO_TRAIN_MAX_LENGTH": "768", "STUDIO_TRAIN_MAX_PROMPT_LENGTH": "600"})
    assert (m.MAX_LENGTH, m.MAX_PROMPT_LENGTH) == (768, 600)


def test_defaults_are_the_8gb_settings():
    m = _load({k: None for k in ("STUDIO_TRAIN_MAX_LENGTH", "STUDIO_TRAIN_BATCH_SIZE",
                                 "STUDIO_TRAIN_GRAD_ACCUM", "STUDIO_TRAIN_MAX_PROMPT_LENGTH")})
    assert (m.MAX_LENGTH, m.BATCH_SIZE, m.GRAD_ACCUM) == (1024, 1, 8)


def test_batch_and_grad_accum_are_configurable():
    m = _load({"STUDIO_TRAIN_BATCH_SIZE": "2", "STUDIO_TRAIN_GRAD_ACCUM": "4"})
    kw = m._training_kwargs("cpu")
    assert kw["per_device_train_batch_size"] == 2
    assert kw["gradient_accumulation_steps"] == 4


def test_gradient_checkpointing_is_on_for_cuda_by_default_and_off_elsewhere():
    """It is what makes 4.8 GB of weights + activations fit an 8 GB card; on CPU
    it only buys memory nobody is short of, at ~30% of the speed."""
    m = _load({"STUDIO_TRAIN_GRAD_CHECKPOINT": None})
    assert m._grad_checkpoint_on("cuda") is True
    assert m._grad_checkpoint_on("cpu") is False
    kw = m._training_kwargs("cuda")
    assert kw["gradient_checkpointing"] is True
    # reentrant checkpointing loses the grad graph through frozen base layers
    assert kw["gradient_checkpointing_kwargs"] == {"use_reentrant": False}
    assert "gradient_checkpointing" not in m._training_kwargs("cpu")


@pytest.mark.parametrize("val,expected", [("1", True), ("0", False), ("on", True), ("off", False)])
def test_gradient_checkpointing_env_wins_on_any_device(val, expected):
    m = _load({"STUDIO_TRAIN_GRAD_CHECKPOINT": val})
    assert m._grad_checkpoint_on("cuda") is expected
    assert m._grad_checkpoint_on("cpu") is expected


def test_alloc_conf_default_is_set_but_never_overrides_the_operator(monkeypatch):
    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    T._prep_alloc_env()
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:128")
    T._prep_alloc_env()
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "max_split_size_mb:128"


# ── OOM becomes instructions, not a traceback ────────────────────────────

class _FakeOOM(RuntimeError):
    """torch.cuda.OutOfMemoryError is a RuntimeError subclass; recognition is by
    type NAME + message so it holds across torch versions without importing it."""
    pass


_FakeOOM.__name__ = "OutOfMemoryError"


@pytest.mark.parametrize("exc", [
    _FakeOOM("CUDA out of memory. Tried to allocate 1.50 GiB"),
    RuntimeError("CUDA out of memory. Tried to allocate 256.00 MiB"),
    RuntimeError("MPS backend out of memory (MPS allocated: 9.07 GB)"),
])
def test_oom_is_recognised(exc):
    assert T._is_oom(exc) is True


@pytest.mark.parametrize("exc", [
    ValueError("bad max_length"),
    RuntimeError("expected scalar type BFloat16 but found Float"),
])
def test_non_oom_is_not_mistaken_for_one(exc):
    assert T._is_oom(exc) is False


def test_guard_oom_raises_an_actionable_systemexit_naming_every_lever():
    def boom():
        raise _FakeOOM("CUDA out of memory. Tried to allocate 1.50 GiB")

    with pytest.raises(SystemExit) as ei:
        T._guard_oom("training", "cuda", "bfloat16", boom)
    msg = str(ei.value)
    assert "OUT OF MEMORY while training" in msg
    for lever in ("STUDIO_TRAIN_DTYPE", "STUDIO_TRAIN_MAX_LENGTH",
                  "STUDIO_TRAIN_BATCH_SIZE", "STUDIO_TRAIN_GRAD_CHECKPOINT",
                  "STUDIO_TRAIN_DEVICE=cpu", "nvidia-smi"):
        assert lever in msg
    # and it says nothing was lost — the cursor only advances after a publish
    assert "cursor did not move" in msg


def test_oom_in_fp32_points_at_the_dtype_first():
    """fp32 is the only case where changing dtype actually halves the weights;
    bf16 -> fp16 is 2 bytes either way and must not be sold as a memory fix."""
    with pytest.raises(SystemExit) as ei:
        T._guard_oom("loading the base model", "cuda", "float32",
                     lambda: (_ for _ in ()).throw(_FakeOOM("out of memory")))
    msg = str(ei.value)
    assert "YOU ARE IN fp32" in msg and "9.6 -> 4.8 GB" in msg
    bf16_msg = T._oom_hint("training", "cuda", "bfloat16")
    assert "SAME 2 bytes" in bf16_msg


def test_guard_oom_never_swallows_a_real_bug():
    def boom():
        raise ValueError("shape mismatch")

    with pytest.raises(ValueError):
        T._guard_oom("training", "cuda", "bfloat16", boom)


def test_guard_oom_returns_the_value_when_nothing_explodes():
    assert T._guard_oom("training", "cpu", "float32", lambda a: a + 1, 41) == 42


# ── the pre-flight verdict a 6 GB card gets BEFORE 4.8 GB is loaded ──────

def test_small_card_is_told_it_will_not_fit_before_it_tries():
    lines = " ".join(T._vram_advice(4.0, "bfloat16"))
    assert "too small for this base" in lines and "STUDIO_TRAIN_DEVICE=cpu" in lines


def test_six_gb_card_gets_the_specific_knobs():
    lines = " ".join(T._vram_advice(6.0, "float16"))
    assert "STUDIO_TRAIN_GRAD_CHECKPOINT=1" in lines
    assert "STUDIO_TRAIN_MAX_LENGTH=512" in lines


def test_eight_gb_card_is_told_the_defaults_are_for_it():
    lines = " ".join(T._vram_advice(8.0, "bfloat16"))
    assert "8 GB class" in lines and "grad-accum" in lines


def test_fp32_is_flagged_as_the_wrong_dtype_for_a_laptop_gpu():
    """fp32 is the ONE dtype change that is really a memory change: 9.6 -> 4.8 GB.
    On an 8 GB card it is also the reason nothing fits."""
    lines = " ".join(T._vram_advice(8.0, "float32"))
    assert "9.6" in lines and "STUDIO_TRAIN_DTYPE=bf16" in lines
    assert "too small for this base" in lines


def test_big_card_is_invited_to_go_faster():
    lines = " ".join(T._vram_advice(24.0, "bfloat16"))
    assert "STUDIO_TRAIN_BATCH_SIZE" in lines


# ── grad-accum: the bar is not frozen, it is accumulating ────────────────

def test_step_plan_matches_the_measured_bootstrap_round():
    """200 samples, batch 1, grad-accum 8 -> 25 optimizer steps."""
    assert T._step_plan(200, 1) == (200, 25)
    assert T._step_plan(200, 2) == (400, 50)
    assert T._step_plan(200, 1, batch_size=2, grad_accum=4) == (100, 25)
    assert T._step_plan(3, 1) == (3, 1)          # never zero steps


def test_short_rounds_log_every_step():
    assert T._log_every(25) == 1
    assert T._log_every(400) == 10


def test_step_plan_is_printed_and_says_a_still_bar_is_not_a_hang(capsys):
    micro, steps = T._print_step_plan(200, "samples", 1)
    out = capsys.readouterr().out
    assert (micro, steps) == (200, 25)
    assert "25 optimizer steps" in out
    assert "OPTIMIZER steps" in out and "NOT a hang" in out


# ── the 4.8 GB download says so before it blocks ─────────────────────────

def _hf_env(monkeypatch, home):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(home))


def test_uncached_model_announces_size_destination_and_the_offline_flag(tmp_path, monkeypatch, capsys):
    _hf_env(monkeypatch, tmp_path)
    T.announce_model_fetch("microsoft/bitnet-b1.58-2B-4T-bf16")
    out = capsys.readouterr().out
    assert "4.8 GB" in out
    assert str(tmp_path) in out                  # WHERE it lands
    assert "HF_HUB_OFFLINE=1" in out             # how the next run skips the hub
    assert "not a hang" in out.lower() or "not training" in out.lower()
    # free space is reported even though the cache dir does not exist yet
    assert "free space on the cache volume" in out


def test_cached_model_says_no_download_expected(tmp_path, monkeypatch, capsys):
    hub = tmp_path / "hub"
    (hub / "models--microsoft--bitnet-b1.58-2B-4T-bf16").mkdir(parents=True)
    _hf_env(monkeypatch, tmp_path)
    T.announce_model_fetch("microsoft/bitnet-b1.58-2B-4T-bf16")
    out = capsys.readouterr().out
    assert "already in the HF cache" in out and "no download expected" in out


def test_cache_root_follows_the_hubs_own_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "explicit"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "home"))
    assert T._hf_cache_root() == str(tmp_path / "explicit")
    _hf_env(monkeypatch, tmp_path / "home")
    assert T._hf_cache_root() == os.path.join(str(tmp_path / "home"), "hub")


def test_announce_never_breaks_a_round(monkeypatch, capsys):
    """Diagnostics must not be able to kill training."""
    monkeypatch.setattr(T, "_hf_cache_root", lambda: (_ for _ in ()).throw(OSError("no home")))
    T.announce_model_fetch("microsoft/bitnet-b1.58-2B-4T-bf16")
    assert "could not inspect" in capsys.readouterr().out


def test_a_local_directory_base_model_is_not_a_download(tmp_path, capsys):
    T.announce_model_fetch(str(tmp_path))
    assert "no download" in capsys.readouterr().out
