"""Self-hosted vLLM serving on Modal.

Serves the active profile from ``config/model_profiles.yaml`` behind vLLM's
API-key check. Run from the repository root (see ``deploy/README.md``):

    modal run -m deploy.modal_vllm::download_model   # weights into the volume, once
    modal serve -m deploy.modal_vllm                 # temporary development URL
    modal deploy -m deploy.modal_vllm                # stable URL

Every command here starts billable containers; none run in tests.
"""

import os
import subprocess

import modal

# This module is imported locally (to define the app) and again inside each
# container. The loader and the profile file ship with the image at the same
# relative paths, so PROFILES_PATH resolves on both sides.
from llm.profiles import PROFILES_PATH, load_profile, vllm_serve_args

PROFILE = load_profile()

# Existing Modal resource names: renaming them would re-download the weights
# or orphan the secret.
APP_NAME = "adops-llm"
SECRET_NAME = "adops-llm-key"  # provides LLM_API_KEY
HF_CACHE_VOLUME = "hf-cache"
VLLM_CACHE_VOLUME = "vllm-cache"
PORT = 8000
SCALEDOWN_SECONDS = 5 * 60
STARTUP_TIMEOUT_SECONDS = 15 * 60
CONTAINER_TIMEOUT_SECONDS = 60 * 60

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install(
        f"vllm=={PROFILE.vllm_version}",
        f"transformers=={PROFILE.transformers_version}",
    )
    .env({
        # Successor of the deprecated HF_HUB_ENABLE_HF_TRANSFER for the hf-xet backend.
        "HF_XET_HIGH_PERFORMANCE": "1",
        # The FlashInfer sampler JIT-compiles a kernel on the first generation
        # and the slim image has no nvcc; without this the server crashes.
        "VLLM_USE_FLASHINFER_SAMPLER": "0",
    })
    .add_local_python_source("llm")
    .add_local_file(PROFILES_PATH, "/root/config/model_profiles.yaml")
)

hf_cache = modal.Volume.from_name(HF_CACHE_VOLUME, create_if_missing=True)
vllm_cache = modal.Volume.from_name(VLLM_CACHE_VOLUME, create_if_missing=True)
VOLUMES = {"/root/.cache/huggingface": hf_cache, "/root/.cache/vllm": vllm_cache}

app = modal.App(APP_NAME)


@app.function(image=image, volumes=VOLUMES, timeout=60 * 60)
def download_model() -> None:
    """Fetch the pinned weights into the cache volume on a CPU container."""
    from huggingface_hub import snapshot_download

    path = snapshot_download(PROFILE.model_id, revision=PROFILE.model_revision)
    hf_cache.commit()
    print(f"{PROFILE.model_id}@{PROFILE.model_revision} saved to {path}")


@app.function(
    image=image,
    gpu=PROFILE.gpu,
    volumes=VOLUMES,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    scaledown_window=SCALEDOWN_SECONDS,
    timeout=CONTAINER_TIMEOUT_SECONDS,
)
@modal.concurrent(max_inputs=4)
@modal.web_server(port=PORT, startup_timeout=STARTUP_TIMEOUT_SECONDS)
def serve() -> None:
    """OpenAI-compatible server; requests without the bearer key are rejected."""
    cmd = [*vllm_serve_args(PROFILE, PORT), "--api-key", os.environ["LLM_API_KEY"]]
    subprocess.Popen(cmd)
