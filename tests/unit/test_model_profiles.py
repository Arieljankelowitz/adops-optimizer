"""The model profile file and its typed loader."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from llm.profiles import (
    PROFILES_PATH,
    UNRESOLVED_IMAGE_DIGEST,
    ModelProfiles,
    load_profile,
    load_profiles,
    vllm_serve_args,
)


def _raw() -> dict:
    return yaml.safe_load(PROFILES_PATH.read_text(encoding="utf-8"))


def _with(**changes: object) -> dict:
    raw = _raw()
    raw["profiles"][raw["active_profile"]].update(changes)
    return raw


def test_repository_profiles_load() -> None:
    profiles = load_profiles()
    assert profiles.active_profile == "qwen38_fp8_l40s_v1"
    assert set(profiles.profiles) == {
        "qwen38_fp8_l40s_v1", "qwen36_fp8_l40s_v1", "granite42_8b_a10_v1"}
    for key, profile in profiles.profiles.items():
        assert profile.profile_id == key
        assert profile.served_model_name == "adops-analysis"
        assert profile.enable_thinking is False
        assert profile.image_digest == UNRESOLVED_IMAGE_DIGEST
        assert not profile.image_digest_resolved


def test_primary_profile_carries_the_proven_pins() -> None:
    primary = load_profile()
    assert primary.model_id == "Qwen/Qwen3.8-27B-FP8"
    assert primary.model_revision == "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
    assert primary.vllm_version == "0.32.0"
    # 5.8.0 is incompatible with vLLM 0.32.0, which needs >=5.16.1,<5.20.
    assert primary.transformers_version == "5.19.0"
    assert primary.gpu == "L40S"
    assert (primary.max_prompt_tokens, primary.max_output_tokens, primary.max_model_len) == (
        12288, 2048, 16384)


def test_fallback_profiles() -> None:
    contingency = load_profile("qwen36_fp8_l40s_v1")
    assert contingency.model_id == "Qwen/Qwen3.6-27B-FP8"
    assert contingency.model_revision != load_profile().model_revision
    granite = load_profile("granite42_8b_a10_v1")
    assert granite.gpu == "A10"
    assert (granite.max_prompt_tokens, granite.max_output_tokens, granite.max_model_len) == (
        6144, 1024, 8192)
    assert granite.reasoning_parser is None


def test_primary_serve_args_match_the_proven_launch() -> None:
    assert vllm_serve_args(load_profile(), 8000) == [
        "vllm", "serve", "Qwen/Qwen3.8-27B-FP8",
        "--revision", "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a",
        "--served-model-name", "adops-analysis",
        "--host", "0.0.0.0", "--port", "8000",
        "--max-model-len", "16384",
        "--max-num-seqs", "1",
        "--gpu-memory-utilization", "0.88",
        "--enforce-eager",
        "--reasoning-parser", "qwen3",
    ]


def test_auto_tool_choice_adds_the_parser() -> None:
    profile = ModelProfiles.model_validate(_with(auto_tool_choice=True)).get()
    assert vllm_serve_args(profile, 8000)[-3:] == [
        "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder"]


@pytest.mark.parametrize("changes", [
    {"enable_thinking": True},
    {"model_revision": "main"},
    {"transformers_version": ">=5.8.0"},
    {"vllm_version": "0.32"},
    {"image_digest": "latest"},
    {"gpu": "H100"},
    {"text_only": False},
    {"max_output_tokens": 8192},
    {"gpu_memory_utilization": 1.2},
    {"enforce_eager": "yes"},
    {"unexpected": 1},
    {"auto_tool_choice": True, "tool_call_parser": None},
    {"auto_tool_choice": False, "manual_tool_request_json_fallback": False},
    {"profile_id": "other_profile"},
], ids=lambda c: ",".join(c))
def test_invalid_profiles_are_rejected(changes: dict) -> None:
    with pytest.raises(ValidationError):
        ModelProfiles.model_validate(_with(**changes))


def test_resolved_image_digest_is_accepted() -> None:
    profile = ModelProfiles.model_validate(_with(image_digest="sha256:" + "a" * 64)).get()
    assert profile.image_digest_resolved


def test_active_profile_must_exist(tmp_path: Path) -> None:
    raw = _raw()
    raw["active_profile"] = "missing_profile"
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_profiles(path)


def test_unknown_profile_id_is_an_error() -> None:
    with pytest.raises(KeyError):
        load_profile("missing_profile")


def test_profile_hash_tracks_every_setting() -> None:
    primary = load_profile()
    assert primary.sha256() == load_profile().sha256()
    changed = ModelProfiles.model_validate(_with(temperature=0.3)).get()
    assert changed.sha256() != primary.sha256()
