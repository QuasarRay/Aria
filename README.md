# Aria research snapshot mirror

This repository mirrors the anonymized Aria research code from
https://anonymous.4open.science/r/CoqProver-Code-57BB
into the [upstream/](upstream/) directory.

GitHub Actions invokes a **Dagger + Python** pipeline daily at 04:17 UTC
and on manual `workflow_dispatch`. The pipeline verifies its own Python
logic, downloads the anonymous repository ZIP in a Dagger container,
validates and extracts it, and pushes changes only when the source files differ.

The upstream content is stored beneath `upstream/` so its GitHub Actions
workflows cannot run in this repository. This is a **file-snapshot mirror**:
it cannot recover the original Git history or files omitted/anonymized
by the anonymous hosting service. Upstream source files are never executed.

Workflow: [.github/workflows/aria-mirror.yml](.github/workflows/aria-mirror.yml).
Pipeline: [mirror/](mirror/).

For local use (requires Docker):
```sh
python -m pip install dagger-io==0.21.10
python mirror/pipeline.py test
python mirror/pipeline.py fetch --export .mirror-stage
python mirror/apply.py --stage .mirror-stage --destination upstream
```

A failed/expired source download never replaces the existing snapshot.
No live download was verified from the development environment because
the anonymous host failed DNS resolution there. Review upstream licensing
and any anonymization constraints before redistribution.
