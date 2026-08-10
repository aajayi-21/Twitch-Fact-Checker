#!/usr/bin/env bash
# Install the GPU speech-to-text backend (torch + transformers) into this
# project's uv environment, picking the right PyTorch wheel for your hardware.
#
#   ./scripts/install_stt_gpu.sh          # auto-detect (cuda / rocm / xpu / cpu)
#   ./scripts/install_stt_gpu.sh xpu      # force Intel Arc / Core Ultra iGPU
#   ./scripts/install_stt_gpu.sh rocm6.4  # force AMD ROCm
#   ./scripts/install_stt_gpu.sh cu128    # force a specific CUDA build
#   ./scripts/install_stt_gpu.sh cpu      # torch backend on CPU (for testing)
#
# Run `uv pip install --help` for the full list of accepted backends.
#
# Why this is not part of `uv sync`: PyTorch publishes a different wheel per
# accelerator from a different index, so no single locked version is correct
# for every machine. uv's --torch-backend inspects the local GPU and picks the
# matching index. run.sh uses `uv sync --inexact`, which leaves these packages
# installed.
set -euo pipefail

cd "$(dirname "$0")/.."

BACKEND="${1:-auto}"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. See https://astral.sh/uv" >&2
    exit 1
fi

# Make sure the project venv exists before installing into it.
uv sync --inexact

# Installed with `uv pip` rather than `uv sync --extra gpu` on purpose: the
# extra's locked torch is the default PyPI (CUDA) build, so syncing it would
# download multiple GB before we replaced it with the right accelerator wheel.
echo "Installing torch/transformers with --torch-backend=${BACKEND} …"
uv pip install --torch-backend="${BACKEND}" torch transformers accelerate

echo
echo "Installed:"
uv run --no-sync python - <<'PY'
import torch

print(f"  torch        {torch.__version__}")
print(f"  cuda avail   {torch.cuda.is_available()}")
if getattr(torch.version, "hip", None):
    print(f"  rocm/hip     {torch.version.hip}")
print(f"  xpu avail    {getattr(getattr(torch, 'xpu', None), 'is_available', lambda: False)()}")
PY

echo
echo "Now set the backend in backend/.env:"
echo "    STT_BACKEND=torch"
echo "    WHISPER_DEVICE=auto          # or cuda / rocm / xpu / cpu"
echo "    WHISPER_MODEL=openai/whisper-small.en   # a Hugging Face repo id"
