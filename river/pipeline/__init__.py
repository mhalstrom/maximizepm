"""Pipelines: the stages of a release, where each version runs, and what each stage run did (#6112).

A layer over the forges (GitHub, GitLab, Gitea and Forgejo, Pulumi Delivery, or no forge), decided by mark 2026-11-18 (canvas
release-tracking of #6110). This package keeps its own tables (schema.py, which core.SCHEMA includes) and talks to
the rest of MaximizePM only through events and a few calls (core.tx, core.now, core.RiverError), so it can move to a
project of its own later.

- store.py: the records (pipeline, stage, environment, version, stage run, step, test suite, test result, deploy)
  and the one function that changes them from events (ingest), plus the reads of the view.
- cdevents.py: the events in the CDEvents form (dev.cdevents.<subject>.<predicate>.<version>).
- steps.py: the '::step start|end' lines that a stage's command prints, as taskrun events.
- junit.py: a JUnit XML test report, as testsuiterun and testcaserun events.

The package's __init__ imports nothing: core imports schema.py while core itself loads.
"""
