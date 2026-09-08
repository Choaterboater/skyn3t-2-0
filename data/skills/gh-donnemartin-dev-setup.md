---
slug: gh-donnemartin-dev-setup
title: donnemartin/dev-setup: OS X 10.10/10.11-era Developer Machine Bootstrap Scripts (stale)
stack: python
tags: cli, python, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-donnemartin-dev-setup
description: "Review hold: Scripts are explicitly scoped by their own README to OS X 10.10 Yosemite / 10.11 El Capitan (both long past end-of-life); last commit 2019-04-13; no evidence in the source that the automation still works against a current macOS release."
license: NOASSERTION
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:09449523c5d5f739c2eba6921743035bbe5b6299e15e30a84fbfa04dc11bc691
  skyn3t-content-sha256: sha256:b11c97b20cc342959f830837f6b76fdc82607b867a737113d574812261a1d8ad
  skyn3t-evidence-index: evidence/reviewed/gh-donnemartin-dev-setup.receipt.json
  skyn3t-evidence-path: evidence/reviewed/b11c97b20cc342959f830837f6b76fdc82607b867a737113d574812261a1d8ad.source
  skyn3t-hold-reason: "Scripts are explicitly scoped by their own README to OS X 10.10 Yosemite / 10.11 El Capitan (both long past end-of-life); last commit 2019-04-13; no evidence in the source that the automation still works against a current macOS release."
  skyn3t-pinned-revision: d86102d761afa5cc1853078200b844212f99e234
  skyn3t-previous-body-sha256: sha256:bbe7ade2f640809aed67d311b053705f24bb2b52967c23bbdee41e4bebf16c32
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/donnemartin/dev-setup
---

Review hold: Scripts are explicitly scoped by their own README to OS X 10.10 Yosemite / 10.11 El Capitan (both long past end-of-life); last commit 2019-04-13; no evidence in the source that the automation still works against a current macOS release.

Applicability: a reference catalog (not an all-in-one installer) of shell scripts and Homebrew-based instructions for bootstrapping a dev workstation: command-line tools, dotfiles, a Python data-analysis stack, Spark/AWS/Heroku, common data stores, JS web dev, and Android dev.

Why held: the README explicitly scopes the scripts as "tested on OS X 10.10 Yosemite and 10.11 El Capitan" (Apache-2.0 licensed content by the author; third-party content under its own licenses; last commit 2019-04-13). Both target OS versions are long past end-of-life, and the tool ecosystem it automates (rbenv-based Ruby version management, 2015-2019-era Python virtualenv workflows, older Homebrew Cask conventions) has moved on substantially. There is no way to verify from this text alone that bootstrap.sh/osxprep.sh/brew.sh still work against a current macOS release, making this an obsolete generation-context case rather than a currently safe procedure.

What the source documents (for historical reference only):
- bootstrap.sh syncs the repo's dotfiles to the user's home directory.
- osxprep.sh runs OS X software updates and installs Xcode Command Line Tools.
- brew.sh installs a curated Homebrew formula/cask list.
- osx.sh applies developer-oriented OS X system defaults.
- pydata.sh, aws.sh, datastores.sh, web.sh, android.sh set up the topic-specific tool stacks named above.
- The author states explicitly: "You're not meant to install everything" -- it is meant as an organized reference, with users encouraged to adapt scripts rather than run them wholesale.

Recommendation: treat as historical inspiration for a "curated per-topic bootstrap script" organizational pattern, not as an executable procedure for any current machine.
