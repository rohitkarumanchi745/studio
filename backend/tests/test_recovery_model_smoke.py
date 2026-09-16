import pytest

from scripts import smoke_recovery_model as smoke


def test_validate_completion_accepts_bounded_decision():
    assert smoke.validate_completion({
        "model": "studio-recovery",
        "choices": [{"message": {"role": "assistant", "content":
            '{"decision":"escalate","reason":"authorized schema is insufficient"}'}}],
    }, "studio-recovery") == {
        "decision": "escalate", "reason": "authorized schema is insufficient",
    }


@pytest.mark.parametrize("body", [
    {},
    {"model": "wrong", "choices": [{"message": {"content": "{}"}}]},
    {"model": "studio-recovery", "choices": [{"message": {
        "content": '{"decision":"retry","reason":"x"}', "tool_calls": [{}]}}]},
    {"model": "studio-recovery", "choices": [{"message": {"content": "not json"}}]},
])
def test_validate_completion_rejects_wrong_or_unsafe_shapes(body):
    with pytest.raises(RuntimeError, match="safe decision contract"):
        smoke.validate_completion(body, "studio-recovery")


def test_token_id_check_is_explicit():
    body = {"model": "studio-recovery", "choices": [{"message": {
        "content": '{"decision":"retry","reason":"temporary connection failure"}'}}]}
    with pytest.raises(RuntimeError, match="safe decision contract"):
        smoke.validate_completion(body, "studio-recovery", require_token_ids=True)
    body["prompt_token_ids"] = [1, 2]
    body["choices"][0]["token_ids"] = [3]
    assert smoke.validate_completion(body, "studio-recovery", require_token_ids=True)["decision"] == "retry"
