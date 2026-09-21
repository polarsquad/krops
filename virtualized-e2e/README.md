# Virtualized e2e harness (WireMock), issue #355

Runs the krops reconcile paths against an in-cluster
[WireMock](https://wiremock.org/) instead of a real cloud: controllers (CAPI
infrastructure providers, ACK, ASO, Config Connector) talk to WireMock over
TLS, WireMock serves recorded or hand-written API responses, and assertion
scripts check what arrived. No cloud account, no real credentials, no cloud
resources.

Phase 0 (spikes) proved an interception mechanism per cloud; the findings
live in [docs/wiremock-e2e-spike-findings-aws.md](../../docs/wiremock-e2e-spike-findings-aws.md)
and its Azure/GCP siblings. This tree is Phase 1: the shared harness library
plus one arm per cloud.

## Layout

```
virtualized-e2e/
├── lib/                  Shared harness components, identical across clouds:
│   │                     WireMock manifest templates, the recording
│   │                     sanitizer, assertion helpers, the scenario shape
│   ├── wiremock/         WireMock Namespace/Deployment/Service templates
│   │                     (envsubst-style, see example.env there)
│   ├── scenario-schema.json   Phase 3 scenario/state-machine JSON shape
│   ├── sanitize_recording.py  Phase 2 recording scrubber (literal lists
│   │                          are per cloud, they live in the arm dirs)
│   └── assertions.py     Phase 5 assertion helpers (Deployment available,
│                         WireMock unmatched-request check)
└── <cloud>/              One arm per cloud, carrying only what differs:
    └── wiremock/           the interception mechanism (endpoint overrides,
        ├── kustomization.yaml  CA trust patches, boot stubs) plus the arm's
        ├── patches/            kustomize wiring
        └── README.md           mechanism documentation
```

`aws/` is the reference arm. `gcp/` is the second arm (CoreDNS rewrite +
SAN-matched TLS, with the WIF credential repoint for the auth path). The
Azure arm is a follow-up task.

## What is NOT here yet

- Recordings (Phase 2, needs live runs to record).
- Scenario content (Phase 3; the JSON shape is `lib/scenario-schema.json`).
- mise tasks and the CI workflow (Phase 4; there is deliberately no
  `mise run <cloud>-e2e-virtualized` yet, and nothing here is Flux-reconciled,
  so there are no `flux-ks.yaml` files and nothing under `mgmt/` or
  `workload/` references this tree).
- Assertion scripts (Phase 5; `lib/assertions.py` carries the shared helpers
  those scripts will call).

## Local sanity checks

The manifests are kustomize-buildable and the templates render with example
parameters; `mise run validate` builds every `kustomization.yaml` in this
tree alongside the `mgmt/` and `workload/` overlays. To render the templates
by hand, see `lib/README.md`.
