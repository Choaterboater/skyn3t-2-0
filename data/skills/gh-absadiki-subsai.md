---
slug: gh-absadiki-subsai
title: Subsai: multi-backend Whisper subtitle generation (CLI, Web-UI, Python API)
stack: python
tags: cli, machine-learning, media, python, reference, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-absadiki-subsai
description: "Review hold: Source warns of Python 3.12+ compatibility problems and needs selected model/media/GPU dependencies; requires a separately confirmed compatible environment rather than downgrading the factory or installing broad model packs."
license: GPL-3.0
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:3d3735d98a8eadab76cb036961a88fe847a2471befc26f4b7f2611b73fa53685
  skyn3t-content-sha256: sha256:de5e1c383b2c2c6177bacea4b777c79f402b30775b515ee736ec49b0834e68ff
  skyn3t-evidence-index: evidence/reviewed/gh-absadiki-subsai.receipt.json
  skyn3t-evidence-path: evidence/reviewed/de5e1c383b2c2c6177bacea4b777c79f402b30775b515ee736ec49b0834e68ff.source
  skyn3t-hold-reason: "Source warns of Python 3.12+ compatibility problems and needs selected model/media/GPU dependencies; requires a separately confirmed compatible environment rather than downgrading the factory or installing broad model packs."
  skyn3t-pinned-revision: 5ed78a85d2b868a907c811404f7cd9179db39968
  skyn3t-previous-body-sha256: sha256:5357a249f0bff1f572e7ba8a7214124633ae0fc17a45313f23cfa6939cf87f3c
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/absadiki/subsai
---

Review hold: Source warns of Python 3.12+ compatibility problems and needs selected model/media/GPU dependencies; requires a separately confirmed compatible environment rather than downgrading the factory or installing broad model packs.

Applicability: adding automatic subtitle/transcription generation to a Python project, with a choice of interchangeable Whisper-family backends (openai/whisper, faster-whisper, whisper.cpp, whisperX, whisper-timestamped, stable-ts, or Hugging Face Transformers) behind one common interface, plus optional translation and auto-sync post-processing.

Prerequisites/version boundary: `ffmpeg` must be installed first (apt/pacman/brew/choco/scoop depending on OS). Python 3.10 or 3.11 is recommended; the source warns 3.12+ may have compatibility issues. A Rust toolchain may be needed if `pip` can't fetch a prebuilt `tokenizers` wheel. GPU acceleration needs a CUDA-capable PyTorch install; plain `pip install` commonly resolves the CPU-only build instead.

Pattern (source-backed):
1. Install: `pip install git+https://github.com/absadiki/subsai` (or clone plus `uv pip install -e .` for an editable install; comment out unused backend deps in `requirements.txt` for a minimal install).
2. CLI usage: `subsai <media_file> --model openai/whisper --model-configs '{"model_type": "small"}' --format srt` -- supports batch processing via a text file listing one media path per line, and translation via `--translation-model` with source/target languages.
3. Python API: instantiate `SubsAI()`, create a model with `create_model('openai/whisper', {...})`, call `transcribe(file, model)`, then `.save('out.srt')` on the result.
4. Web-UI: run `subsai-webui` for a fully offline, local browser UI with the same model choices plus subtitle editing, translation, and auto-sync tooling built in.
5. Docker path: `docker pull absadiki/subsai:main` then `docker run --gpus=all -p 8501:8501 -v /path/to/media:/media_files absadiki/subsai:main` (drop `--gpus=all` for CPU-only).

Verification: run the CLI against a short sample media file first and confirm a valid `.srt`/`.vtt` file is produced with plausible timestamps before batch-processing a large media library.

Failure handling: if GPU isn't detected at runtime despite having a supported card, reinstall PyTorch using the CUDA-specific instructions from pytorch.org rather than the default `pip install`, since `pip` frequently resolves the CPU-only wheel by default.
