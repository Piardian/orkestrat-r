# Reliability stack

This layer adds ready-made infrastructure around the existing safety state machine.

## Components

- **Temporal**: durable stage execution and retry. Each existing pipeline stage is a separate Temporal activity.
- **Langfuse**: optional agent/LLM trace collection. It is fail-open and cannot block the pipeline.
- **Sentry**: optional runtime exception/performance reporting. PII sending is disabled by default.
- **GitHub Actions**: Windows/Linux and Python 3.11/3.12 smoke matrix plus an advisory legacy regression job.

## Install

```powershell
python -m pip install -r requirements-reliability.txt
```

## Temporal

Run a Temporal server (local dev server or Temporal Cloud), configure `.env`, then start the worker:

```powershell
python run_temporal_worker.py
```

Run the integrated pipeline through Temporal:

```powershell
python run_integrated.py --repo "C:\path\to\repo" --task "Fix the requested bug" --orchestrator temporal
```

Temporal retries individual stages. The existing `GoalPipelineEngine` remains the source of truth and re-reads persisted state on every activity, so a retried completed stage is skipped instead of repeated.

## Langfuse

Set:

```text
AGENT_ARMY_LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

If Langfuse is unavailable or misconfigured, the pipeline continues without tracing.

## Sentry

Set:

```text
AGENT_ARMY_SENTRY_ENABLED=true
SENTRY_DSN=...
SENTRY_TRACES_SAMPLE_RATE=0.1
```

If Sentry is unavailable or misconfigured, the pipeline continues without reporting.

## Apply safety

Temporal, Langfuse and Sentry do not bypass the existing apply gate. `auto_apply` remains false by default and the existing final-review service still owns the explicit apply decision.
