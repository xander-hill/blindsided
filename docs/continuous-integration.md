# Continuous integration

## Required CI

`.github/workflows/ci.yml` runs for every pull request and every push to
`main`. It contains independent, timeout-bounded jobs for:

- the complete backend test suite;
- Python syntax and changed-file formatting checks;
- deterministic regeneration of the Python and TypeScript protobuf clients;
- Kubernetes manifest tests.

These jobs normally finish in 5–15 minutes in parallel. They are the checks
intended for branch protection. The Compose failure/evaluation suite is not a
required pull-request blocker.

The frontend production build and frontend lint jobs are commented out while
the frontend is being redone. Protobuf drift validation remains enabled because
the generated TypeScript client is part of the shared API contract.

## Manual system evaluation

`.github/workflows/manual-evaluation.yml` runs only through
`workflow_dispatch`. Its matrix starts a fresh Docker Compose stack for each
scenario and covers:

- normal auction flow;
- concurrent bidding;
- backup failure;
- primary failover;
- restart durability;
- watch behavior;
- observability validation.

Each matrix job captures startup output, readiness output, the scenario
timeline, final Compose state, and all service logs. Artifacts are retained for
14 days. Cleanup uses an `always()` step and removes containers, networks,
orphans, and scenario volumes. Scenarios have 3–15 minute command timeouts and
each job has a 30 minute upper bound.

The matrix usually completes in 15–30 minutes because scenarios run in
parallel; cold image pulls or builds can make the first run slower.

Kubernetes scaling (`tools/evaluation/kubernetes_scaling.sh`) is intentionally
excluded from GitHub-hosted CI. It requires a reliable Kubernetes cluster,
metrics-server, working autoscaling metrics, and sufficient permissions. Run it
manually on a self-hosted runner or a known-good development cluster.
