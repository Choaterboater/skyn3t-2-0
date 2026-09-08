---
slug: gh-adilmoujahid-donorschoose-visualization
title: adilmoujahid/DonorsChoose_Visualization: 2016 Vagrant+MongoDB tutorial (held)
stack: react
tags: data-visualization, mongodb, python, reference, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-adilmoujahid-donorschoose-visualization
description: "Review hold: No machine-verified license (GitHub license API: Not Found/404); setup is also tied to an unversioned external dataset download and a 2016 Vagrant/MongoDB workflow with no verification step, so it cannot be safely generalized without inventing missing checks."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:148aec412b54ba5a21f528f0abb3fb96ddb9c0a61189f118e2b1ddae3edb11fc
  skyn3t-content-sha256: sha256:947251c2c699d6a83f5865f39d834d2ebb1501eee9190c3ca113bb6b361cf452
  skyn3t-evidence-index: evidence/reviewed/gh-adilmoujahid-donorschoose-visualization.receipt.json
  skyn3t-evidence-path: evidence/reviewed/947251c2c699d6a83f5865f39d834d2ebb1501eee9190c3ca113bb6b361cf452.source
  skyn3t-hold-reason: "No machine-verified license (GitHub license API: Not Found/404); setup is also tied to an unversioned external dataset download and a 2016 Vagrant/MongoDB workflow with no verification step, so it cannot be safely generalized without inventing missing checks."
  skyn3t-pinned-revision: 1b0305e2b2c70cab0a135cd179b9cf7748d3a029
  skyn3t-previous-body-sha256: sha256:8da1c51087d267f06d9f7fc389fb643a30fd7f9926650753854cc5924858dbf8
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/adilmoujahid/donorschoose_visualization
---

Review hold: No machine-verified license (GitHub license API: Not Found/404); setup is also tied to an unversioned external dataset download and a 2016 Vagrant/MongoDB workflow with no verification step, so it cannot be safely generalized without inventing missing checks.

This repository accompanies a 2015 blog post on interactive data visualization with D3.js, DC.js, Python, and MongoDB. Its documented setup is: `pip install -r requirements.txt`, `vagrant up` to boot a MongoDB VM, then download a specific dataset (`wget https://s3.amazonaws.com/open_data/csv/opendata_projects.zip`) and import it with `mongoimport -d donorschoose -c projects --type csv --file /vagrant/opendata_projects.csv -headerline`.

This record is held rather than activated. The GitHub license API returns Not Found for this repository, so its reuse terms are unresolved. Independently, the setup path itself is tightly bound to a specific, unversioned external dataset URL and a Vagrant-provisioned VM workflow from 2016, with no verification step shown in the source and no confirmation the dataset URL or `mongoimport --type csv` flag still work against current MongoDB tooling. Presenting this as a generically reusable data-visualization procedure would require inventing checks not present in the source.
