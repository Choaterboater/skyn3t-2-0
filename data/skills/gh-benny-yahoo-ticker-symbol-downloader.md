---
slug: gh-benny-yahoo-ticker-symbol-downloader
title: Benny-/Yahoo-ticker-symbol-downloader: scraper with a documented history of upstream breakage (held)
stack: python
tags: python, reference, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-benny-yahoo-ticker-symbol-downloader
description: "Review hold: The tool depends on scraping an undocumented, frequently changing third-party endpoint; its own changelog documents 5+ breaking changes from Yahoo API/HTML changes through 2018, with no commits since to track further changes, so functionality cannot be assumed without live verification, which this task must not perform."
license: BSD-3-Clause
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:a0ecbf412dc69f24fbaecc60fe6c0fbd42e517403d8b0a399dedc3bad11a503b
  skyn3t-content-sha256: sha256:0ab0eea8fafa00b5fd27233b5d1a743f7b428e6e906b28362496655384e5a0f8
  skyn3t-evidence-index: evidence/reviewed/gh-benny-yahoo-ticker-symbol-downloader.receipt.json
  skyn3t-evidence-path: evidence/reviewed/0ab0eea8fafa00b5fd27233b5d1a743f7b428e6e906b28362496655384e5a0f8.source
  skyn3t-hold-reason: "The tool depends on scraping an undocumented, frequently changing third-party endpoint; its own changelog documents 5+ breaking changes from Yahoo API/HTML changes through 2018, with no commits since to track further changes, so functionality cannot be assumed without live verification, which this task must not perform."
  skyn3t-pinned-revision: 305835730773ae4000f38dc047bbe4cc41f782f4
  skyn3t-previous-body-sha256: sha256:35c28dce2fe267a78c60326f9122fa1e9388f4d6af86035c187feaf53b82daff
  skyn3t-review-status: held
  skyn3t-source-path: README.rst
  skyn3t-source-url: https://github.com/benny-/yahoo-ticker-symbol-downloader
---

Review hold: The tool depends on scraping an undocumented, frequently changing third-party endpoint; its own changelog documents 5+ breaking changes from Yahoo API/HTML changes through 2018, with no commits since to track further changes, so functionality cannot be assumed without live verification, which this task must not perform.

This tool scrapes Yahoo Finance's lookup pages to produce ticker-symbol lists (csv/xlsx/json/yaml) for stocks, ETFs, futures, indexes, mutual funds, currencies, warrants, and bonds, installable via `pip install Yahoo-ticker-downloader` and run as `YahooTickerDownloader.py [type]` with resumable downloads and export filtering by exchange.

This record is held rather than activated. The project's own changelog documents at least five separate breaking changes forced by changes to Yahoo's API/HTML between 2014 and 2018 (switches between JSON APIs, HTML scraping, and a `searchassist` API, plus a robots.txt compliance check the maintainer notes was added reluctantly, calling it an "anti-feature"), and there have been no commits since December 2018 to track any further upstream changes since. Given this specific, source-documented history of fragility against an undocumented third-party endpoint, and no way to verify current functionality without making a live network request (out of scope for this task), the documented commands cannot be safely presented as still-functional reusable guidance.
