# Third-party software and media in this extension

A licence for the extension's own code has not been chosen yet. Whatever it
turns out to be, the extension ships with, or downloads, the following work
by others, each under its own terms.

## Shipped in the package

| Component | Version | Licence | Where |
|---|---|---|---|
| [Preact](https://github.com/preactjs/preact) | 10.27.1 | MIT | `vendor/preact/` |
| [htm](https://github.com/developit/htm) | 3.1.1 | Apache-2.0 | `vendor/htm/` |
| [Transformers.js](https://github.com/huggingface/transformers.js) | 4.3.2 | Apache-2.0 (text in `vendor/transformers/LICENSE-transformers.txt`) | `vendor/transformers/transformers.js` |
| [ONNX Runtime Web](https://github.com/microsoft/onnxruntime) | 1.31.0-dev.20260914 | MIT | `vendor/transformers/ort-wasm-simd-threaded.asyncify.*` |

Exact versions and integrity hashes are pinned in `scripts/vendor_ui_runtime.py`
and `scripts/vendor_stt_runtime.py`, which are the only way these files are
produced. They are unmodified apart from import paths rewritten to relative
ones (Preact hooks, htm).

ONNX Runtime — Copyright (c) Microsoft Corporation. MIT License:

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

Preact — Copyright (c) 2015-present Jason Miller. MIT License (same terms as
above).

## Downloaded on first use (not in the package)

Speech-recognition model weights are fetched from Hugging Face at a pinned
revision when the viewer sets up in-browser speech. Each is under its own
licence, stated on its model page:

- <https://huggingface.co/onnx-community/moonshine-base-ONNX>
- <https://huggingface.co/onnx-community/whisper-base.en>
- <https://huggingface.co/onnx-community/parakeet-ctc-0.6b-ONNX>

## Audio

- `engine/stt/assets/selftest_16k.wav` — an eleven-second excerpt of President
  John F. Kennedy's 1961 inaugural address. A work of the United States
  federal government; public domain.
- `engine/demo/demo_16k.wav` — synthesised speech, generated with
  [eSpeak NG](https://github.com/espeak-ng/espeak-ng) from a script written for
  this project (`backend/scripts/make_fixture_wav.py`).
