#!/usr/bin/env python3
"""Vendor the in-browser speech-to-text runtime into ``extension/vendor``.

Manifest V3 forbids remotely hosted code, so the extension cannot load
Transformers.js or the ONNX Runtime WebAssembly binary from a CDN (which is
what both libraries do by default). This script downloads the two pinned npm
tarballs, verifies them against the registry's published SHA-512 integrity
hashes, and extracts only the files the extension needs:

- ``transformers.js`` — the self-contained browser bundle (ONNX Runtime Web's
  JavaScript is already inlined; no bare module imports).
- ``ort-wasm-simd-threaded.asyncify.mjs`` / ``.wasm`` — the ONNX Runtime
  WebAssembly module that bundle loads at run time. One binary serves both the
  WebGPU and the CPU (WASM) execution providers.

The output directory is git-ignored (the ``.wasm`` alone is ~27 MB); re-run
this script after cloning or when bumping a version below.

Usage (from the repo root)::

    python3 scripts/vendor_stt_runtime.py
    python3 scripts/vendor_stt_runtime.py --force   # re-download and overwrite
"""

import argparse
import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "extension" / "vendor" / "transformers"

DOWNLOAD_TIMEOUT_S = 300


@dataclass(frozen=True)
class PinnedPackage:
    """One npm tarball, pinned by version and registry integrity hash."""

    name: str
    version: str
    tarball_url: str
    # The registry's ``dist.integrity`` value: "sha512-<base64 digest>".
    integrity: str
    # Tarball member path -> file name written into the output directory.
    members: dict[str, str]


# The ONNX Runtime version MUST match the one Transformers.js was built
# against (see ``dependencies["onnxruntime-web"]`` in its package.json): the
# JavaScript glue and the WebAssembly binary are a matched pair.
PINNED_PACKAGES: tuple[PinnedPackage, ...] = (
    PinnedPackage(
        name="@huggingface/transformers",
        version="4.3.2",
        tarball_url=(
            "https://registry.npmjs.org/@huggingface/transformers/-/"
            "transformers-4.3.2.tgz"
        ),
        integrity=(
            "sha512-V3+dqf3iWlRYJ9I9GreM3zGQd0lAXnnoPeddmkOQYWAEE4l/+Fi6p7tx/"
            "qVUJlKQgLXGim2W76rTOzz+eC0i7g=="
        ),
        members={
            "package/dist/transformers.js": "transformers.js",
            "package/LICENSE": "LICENSE-transformers.txt",
        },
    ),
    PinnedPackage(
        name="onnxruntime-web",
        version="1.31.0-dev.20260914-8d85527a0",
        tarball_url=(
            "https://registry.npmjs.org/onnxruntime-web/-/"
            "onnxruntime-web-1.31.0-dev.20260914-8d85527a0.tgz"
        ),
        integrity=(
            "sha512-Iy7rtadoBgxS/LLvDr3QW38DB1PNXRnr0GJMcL0TAt7c9qjgVQl83UlGCVyeAnK2"
            "InpmW8Uc3PL8XIuqtDeF6g=="
        ),
        members={
            "package/dist/ort-wasm-simd-threaded.asyncify.mjs": (
                "ort-wasm-simd-threaded.asyncify.mjs"
            ),
            "package/dist/ort-wasm-simd-threaded.asyncify.wasm": (
                "ort-wasm-simd-threaded.asyncify.wasm"
            ),
            # The tarball ships no LICENSE file; the package is MIT-licensed
            # (https://github.com/microsoft/onnxruntime/blob/main/LICENSE) and
            # the notice must be added by hand before a store release.
        },
    ),
)


class IntegrityError(RuntimeError):
    """A downloaded tarball did not match its pinned integrity hash."""


def download(url: str) -> bytes:
    """Fetch ``url`` fully into memory (tarballs here are at most ~35 MB)."""
    request = urllib.request.Request(url, headers={"User-Agent": "vendor-script"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
        return response.read()


def verify_integrity(payload: bytes, integrity: str, package_name: str) -> None:
    """Raise :class:`IntegrityError` unless ``payload`` matches ``integrity``."""
    algorithm, _, expected_b64 = integrity.partition("-")
    if algorithm != "sha512":
        raise IntegrityError(f"{package_name}: unsupported integrity algorithm")
    actual_b64 = base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")
    if actual_b64 != expected_b64:
        raise IntegrityError(
            f"{package_name}: SHA-512 mismatch — refusing to extract "
            f"(expected {expected_b64[:16]}…, got {actual_b64[:16]}…)"
        )


def extract_members(payload: bytes, package: PinnedPackage, output: Path) -> None:
    """Write the wanted tarball members into ``output`` under their new names."""
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member_path, output_name in package.members.items():
            try:
                member = archive.getmember(member_path)
            except KeyError as error:
                raise RuntimeError(
                    f"{package.name}@{package.version}: {member_path} is missing "
                    "from the tarball"
                ) from error
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"{package.name}: {member_path} is not a file")
            (output / output_name).write_bytes(extracted.read())
            print(f"  wrote {output_name} ({member.size / 1e6:.1f} MB)")


def write_manifest(output: Path) -> None:
    """Record what was vendored so a stale directory is easy to spot."""
    lines = ["Vendored by scripts/vendor_stt_runtime.py — do not edit by hand.", ""]
    lines += [f"{package.name}@{package.version}" for package in PINNED_PACKAGES]
    (output / "VERSIONS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_up_to_date(output: Path) -> bool:
    """True when every expected file exists and the version stamp matches."""
    stamp = output / "VERSIONS.txt"
    if not stamp.is_file():
        return False
    stamped = stamp.read_text(encoding="utf-8")
    expected_files = [name for pkg in PINNED_PACKAGES for name in pkg.members.values()]
    return all(
        f"{package.name}@{package.version}" in stamped for package in PINNED_PACKAGES
    ) and all((output / name).is_file() for name in expected_files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--force", action="store_true", help="re-download even if up to date"
    )
    args = parser.parse_args()
    output: Path = args.output

    if not args.force and is_up_to_date(output):
        print(f"{output} is already up to date (use --force to re-download).")
        return 0

    output.mkdir(parents=True, exist_ok=True)
    for package in PINNED_PACKAGES:
        print(f"{package.name}@{package.version}")
        try:
            payload = download(package.tarball_url)
            verify_integrity(payload, package.integrity, package.name)
            extract_members(payload, package, output)
        except (OSError, IntegrityError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    write_manifest(output)
    print(f"Done: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
