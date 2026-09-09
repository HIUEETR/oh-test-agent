# Runtime artifacts

This directory is reserved for locally generated execution evidence and is not source code.

Typical content includes:

- `preflight/`: HDC, screenshot, dependency, model-configuration, and Hypium checks.
- `runs/<run-id>/`: Run Trace JSON, screenshots, UI hierarchy, commands, generated Hypium files, replay evidence, and HTML reports.
- `validation/`: temporary browser profiles and visual validation screenshots.
- `agent.db*`: local SQLite metadata and journals.
- `phase1/`: preserved raw evidence from the legacy feasibility probe.

Git tracks this README only. Reusable, reviewed, non-secret fixtures are stored under
`tests/fixtures/legacy/zhihu-plus/` with a manifest and SHA-256 digests. Do not force-add runtime screenshots,
logs, databases, browser profiles, or model responses.

Use `scripts/clean-runtime.ps1` to remove selected generated content. The script deliberately never removes
`artifacts/phase1`.
