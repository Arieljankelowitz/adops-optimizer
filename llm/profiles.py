"""Typed loader for ``config/model_profiles.yaml``.

The serving deployment and the client read the same file, so the model they
agree on is never hard-coded in two places. This module depends only on the
standard library, pydantic and PyYAML because it is also imported inside the
serving container.
"""

import hashlib
import json
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILES_PATH = Path(__file__).resolve().parents[1] / "config" / "model_profiles.yaml"
UNRESOLVED_IMAGE_DIGEST = "REQUIRED_AFTER_COMPATIBILITY_TEST"

_EXACT_VERSION = r"^\d+\.\d+\.\d+$"
_NAME = r"^[a-z0-9][a-z0-9_]*$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ModelProfile(_Frozen):
    profile_id: str = Field(pattern=_NAME)
    status: Literal["serving_smoke_tested", "untested"]
    model_id: str = Field(pattern=r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")
    # Immutable Hugging Face commit, never a branch name.
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    served_model_name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    license: str = Field(min_length=1)
    quantization: Literal["official_checkpoint_fp8", "official_checkpoint_bf16"]
    vllm_version: str = Field(pattern=_EXACT_VERSION)
    transformers_version: str = Field(pattern=_EXACT_VERSION)
    image_digest: str = Field(pattern=rf"^(sha256:[0-9a-f]{{64}}|{UNRESOLVED_IMAGE_DIGEST})$")
    gpu: Literal["L40S", "A10"]
    tensor_parallel_size: int = Field(ge=1)
    text_only: Literal[True]
    max_model_len: int = Field(gt=0)
    max_num_seqs: int = Field(ge=1)
    gpu_memory_utilization: float = Field(gt=0, lt=1)
    enforce_eager: bool
    max_prompt_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    temperature: float = Field(ge=0, le=2)
    # Every JSON task runs without thinking; the request contract admits no other value.
    enable_thinking: Literal[False]
    reasoning_parser: str | None = Field(default=None, pattern=_NAME)
    tool_call_parser: str | None = Field(default=None, pattern=_NAME)
    auto_tool_choice: bool
    manual_tool_request_json_fallback: bool

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.max_prompt_tokens + self.max_output_tokens > self.max_model_len:
            raise ValueError("max_prompt_tokens + max_output_tokens exceed max_model_len")
        if self.auto_tool_choice and self.tool_call_parser is None:
            raise ValueError("auto_tool_choice needs a tool_call_parser")
        if not (self.auto_tool_choice or self.manual_tool_request_json_fallback):
            raise ValueError("profile has no tool-request path")
        return self

    @property
    def image_digest_resolved(self) -> bool:
        return self.image_digest != UNRESOLVED_IMAGE_DIGEST

    def sha256(self) -> str:
        """Hash of every setting, for run manifests."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ModelProfiles(_Frozen):
    active_profile: str = Field(pattern=_NAME)
    profiles: dict[str, ModelProfile] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _inject_ids(cls, data: object) -> object:
        # The YAML keys each profile by its ID instead of repeating it.
        if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
            profiles = {}
            for key, entry in data["profiles"].items():
                if isinstance(entry, dict):
                    if "profile_id" in entry and entry["profile_id"] != key:
                        raise ValueError(
                            f"profile {key!r} declares profile_id {entry['profile_id']!r}"
                        )
                    entry = {"profile_id": key, **entry}
                profiles[key] = entry
            data = {**data, "profiles": profiles}
        return data

    @model_validator(mode="after")
    def _check_active(self) -> Self:
        if self.active_profile not in self.profiles:
            raise ValueError(f"active_profile {self.active_profile!r} is not defined")
        return self

    def get(self, profile_id: str | None = None) -> ModelProfile:
        """The named profile, or the active one; an unknown name is an error."""
        key = self.active_profile if profile_id is None else profile_id
        if key not in self.profiles:
            raise KeyError(f"unknown model profile {key!r}")
        return self.profiles[key]


def load_profiles(path: Path = PROFILES_PATH) -> ModelProfiles:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return ModelProfiles.model_validate(data)


def load_profile(profile_id: str | None = None, path: Path = PROFILES_PATH) -> ModelProfile:
    return load_profiles(path).get(profile_id)


def vllm_serve_args(profile: ModelProfile, port: int) -> list[str]:
    """``vllm serve`` arguments for ``profile``, without the API key."""
    args = [
        "vllm", "serve", profile.model_id,
        "--revision", profile.model_revision,
        "--served-model-name", profile.served_model_name,
        "--host", "0.0.0.0", "--port", str(port),
        "--max-model-len", str(profile.max_model_len),
        "--max-num-seqs", str(profile.max_num_seqs),
        "--gpu-memory-utilization", str(profile.gpu_memory_utilization),
    ]
    if profile.tensor_parallel_size > 1:
        args += ["--tensor-parallel-size", str(profile.tensor_parallel_size)]
    if profile.enforce_eager:
        args.append("--enforce-eager")
    if profile.reasoning_parser:
        args += ["--reasoning-parser", profile.reasoning_parser]
    if profile.auto_tool_choice:
        args += ["--enable-auto-tool-choice", "--tool-call-parser", str(profile.tool_call_parser)]
    return args
