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

# --- Intel detection -------------------------------------------------------
# uv's `auto` probes for an NVIDIA driver and an AMD ROCm arch; it has no
# Intel branch, so on an Intel-only machine `auto` silently resolves to the
# +cpu wheel. Detect Intel ourselves and upgrade `auto` to `xpu`, but only
# when there is no NVIDIA/AMD card to defer to (on a hybrid machine uv's own
# detection is the better answer).
has_intel_gpu() {
    local vendor
    for vendor in /sys/class/drm/card*/device/vendor; do
        [[ -r "$vendor" ]] || continue
        [[ "$(<"$vendor")" == "0x8086" ]] && return 0
    done
    return 1
}

has_discrete_gpu() {
    [[ -e /proc/driver/nvidia/version ]] && return 0
    command -v nvidia-smi >/dev/null 2>&1 && return 0
    [[ -d /sys/module/amdgpu ]] && return 0
    return 1
}

if [[ "$BACKEND" == "auto" ]] && has_intel_gpu && ! has_discrete_gpu; then
    echo "Detected an Intel GPU and no NVIDIA/AMD card — using --torch-backend=xpu."
    echo "(uv's own 'auto' has no Intel branch and would install the CPU wheel.)"
    BACKEND="xpu"
fi

# Intel's compute runtime is a SYSTEM package; the wheels cannot supply it.
# Without the Level Zero loader, torch imports fine and torch.xpu.is_available()
# just returns False — a silent fall back to CPU, so warn early and loudly.
if [[ "$BACKEND" == "xpu" ]] && ! ls /usr/lib/*/libze_loader.so* >/dev/null 2>&1; then
    echo "WARNING: no Level Zero loader found (libze_loader.so)." >&2
    echo "  torch.xpu will report unavailable until you install Intel's compute" >&2
    echo "  runtime, e.g. on Debian/Ubuntu:" >&2
    echo "      sudo apt install libze1 libze-intel-gpu1 intel-opencl-icd" >&2
fi

# Make sure the project venv exists before installing into it.
uv sync --inexact

# Installed with `uv pip` rather than `uv sync --extra gpu` on purpose: the
# extra's locked torch is the default PyPI (CUDA) build, so syncing it would
# download multiple GB before we replaced it with the right accelerator wheel.
#
# --reinstall-package torch is load-bearing: a bare `torch` requirement is
# already satisfied by whatever variant is installed, so without it, re-running
# this script with a DIFFERENT backend is a no-op that reports success while
# leaving the old wheel (e.g. +cpu) in place.
echo "Installing torch/transformers with --torch-backend=${BACKEND} …"
uv pip install --torch-backend="${BACKEND}" --reinstall-package torch \
    torch transformers accelerate

echo
echo "Installed:"
uv run --no-sync python - "$BACKEND" <<'PY'
import sys

import torch

requested = sys.argv[1]
cuda_ok = torch.cuda.is_available()
hip = getattr(torch.version, "hip", None)
xpu_ok = getattr(getattr(torch, "xpu", None), "is_available", lambda: False)()

print(f"  torch        {torch.__version__}")
print(f"  cuda avail   {cuda_ok}")
if hip:
    print(f"  rocm/hip     {hip}")
print(f"  xpu avail    {xpu_ok}")
if xpu_ok:
    print(f"  xpu device   {torch.xpu.get_device_name(0)}")
elif cuda_ok:
    print(f"  cuda device  {torch.cuda.get_device_name(0)}")

# The install can "succeed" while leaving you on CPU (wrong wheel, or a
# missing system runtime). Say so plainly rather than letting it surface
# later as mysteriously slow transcription.
if requested not in {"cpu", "auto"} and not (cuda_ok or xpu_ok):
    print()
    print(f"  WARNING: asked for '{requested}' but no accelerator is available.")
    print("  Transcription would silently run on the CPU.")
    if requested == "xpu":
        print("  Check: the wheel above should read '+xpu', and you need Intel's")
        print("  compute runtime (libze1 / libze-intel-gpu1 / intel-opencl-icd).")
    sys.exit(1)
PY

echo
echo "Now set the backend in backend/.env:"
echo "    STT_BACKEND=torch"
echo "    WHISPER_DEVICE=auto          # or cuda / rocm / xpu / cpu"
echo "    WHISPER_MODEL=openai/whisper-small.en   # a Hugging Face repo id"
