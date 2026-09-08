---
slug: gh-hfrost0-bilix
title: HFrost0/bilix: Async Bilibili/Video-Site Downloader CLI + Python API
stack: python
tags: python, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-hfrost0-bilix
description: "Applicability: a fast, async downloader for bilibili (and other supported sites) usable either as a standalone CLI or as an importable async Python API -- useful as a reference pattern for building a concurrency-controlled async download tool. Apache-2.0 licensed, last commit 2024-09-20."
license: Apache-2.0
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:a958f2babb308ad8956113b36f6e077e1e4ef39086c26d6b4dd2fd5afce93311
  skyn3t-content-sha256: sha256:fb931e65e6b1cb5fa85fdff4e5179112c17ef294706ad8444c043112b5b087cd
  skyn3t-evidence-index: evidence/reviewed/gh-hfrost0-bilix.receipt.json
  skyn3t-evidence-path: evidence/reviewed/fb931e65e6b1cb5fa85fdff4e5179112c17ef294706ad8444c043112b5b087cd.source
  skyn3t-pinned-revision: bb5b234cdfe3fafc4db9d992b91091f2edf791e5
  skyn3t-previous-body-sha256: sha256:c47656b7ae27030d7e14ed0bd2eb4a51919cd87b7082f7a9cd4d8b92899093b4
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/hfrost0/bilix
---

Applicability: a fast, async downloader for bilibili (and other supported sites) usable either as a standalone CLI or as an importable async Python API -- useful as a reference pattern for building a concurrency-controlled async download tool. Apache-2.0 licensed, last commit 2024-09-20.

Prerequisites: Python (pip) or Homebrew on macOS.

Procedure:
1. Install: pip install bilix (or on macOS, brew install bilix).
2. CLI usage: bilix v 'url' (the v subcommand is a short alias for get_video).
3. Python API usage: import DownloaderBilibili from bilix.sites.bilibili, use it as an async context manager (async with DownloaderBilibili() as d:), and call await d.get_video('url') inside an asyncio.run(main()) entry point.
4. Concurrency and download speed are controllable via the tool's async downloader settings (documented separately per-site in the project).

Verification: after step 2 or 3, confirm the target file was written to disk and matches the expected media (bilix supports submissions, anime/TV series, clips, audio, favourites, danmaku, and covers per its feature list).

Failure handling: if a URL/site isn't recognized, check the project's Issues/Discussions for site-support status before assuming a bug in your own integration code; bilix is explicitly designed to be extended via its Python module structure for additional download scenarios rather than patched ad hoc.
