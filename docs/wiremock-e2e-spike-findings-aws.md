# WireMock e2e spike findings: AWS (issue #355, Phase 0)

Spike date: 2026-09-20. Scope: AWS only. Question: do CAPA and the ACK
controllers honor `AWS_ENDPOINT_URL` (global and/or per-service) pointed at
an in-cluster WireMock, with a throwaway CA for TLS trust, so that cloud API
traffic can be virtualized without network-layer interception?

Short answer: yes for both, with two ACK-specific caveats that are easy to
handle in the Phase 1 harness. No CoreDNS rewrite of real AWS hostnames is
needed anywhere.

## Environment and pinned versions

| Component | Version | Source |
|---|---|---|
| kind | v0.33.0 (node image kindest/node:v1.37.0) | repo mise pin |
| clusterctl | v1.14.2 | repo mise pin |
| cert-manager | v1.21.1 | installed by clusterctl init |
| cluster-api (core/bootstrap/control-plane) | v1.14.2 | installed by clusterctl init |
| CAPA | v2.13.0 | repo pin in `mgmt/aws/capi-providers/capa-system/providers.yaml` |
| ACK S3 controller | chart 1.11.0, image `public.ecr.aws/aws-controllers-k8s/s3-controller:1.11.0` | repo pin in `workload/base/aws-operators/helm.yaml` |
| WireMock | `wiremock/wiremock:3.13.2` | docker hub, latest stable at spike time |
| helm | v4.3.0 | repo mise pin |

Scratch kind cluster `wiremock-aws-spike` (single control-plane node),
created and deleted for this spike. Dummy static credentials throughout (the
well-known AWS documentation EXAMPLE key pair). No real AWS account, no real
credentials, no cloud resources created.

## Setup shape

1. `kind create cluster --name wiremock-aws-spike`.
2. `clusterctl init --infrastructure aws:v2.13.0` with dummy
   `AWS_B64ENCODED_CREDENTIALS` (base64 of a credentials file holding the
   EXAMPLE key pair). clusterctl requires the variable to be set; it does not
   validate the values against AWS.
3. Throwaway CA: openssl root CA (2-day), server certificate signed by it for
   the WireMock Service names plus wildcards (SAN list below), packed into a
   Java keystore. WireMock serves HTTPS on 8443 behind a ClusterIP Service on
   port 443, `wiremock.wiremock-system.svc`.
4. Controller Deployments patched with the endpoint env vars, `SSL_CERT_FILE`
   pointing at the mounted CA, and a `wiremock-ca` ConfigMap volume. The
   ConfigMap must exist in each consuming namespace (volumes cannot cross
   namespaces).
5. Triggers: a CAPI `Cluster` + `AWSCluster` (`spec.region: eu-north-1`) for
   CAPA, and an ACK `Bucket` CR for the S3 controller.
6. Observation: WireMock admin API `/__admin/requests` over a
   `kubectl port-forward` with `curl --cacert`.

## Exact patches applied

CAPA, `deployment/capa-controller-manager` in `capa-system`, container
`manager` (strategic merge patch):

```yaml
spec:
  template:
    spec:
      volumes:
        - name: wiremock-ca
          configMap:
            name: wiremock-ca
      containers:
        - name: manager
          env:
            - name: AWS_ENDPOINT_URL                     # global override
              value: "https://wiremock.wiremock-system.svc"
            - name: AWS_ENDPOINT_URL_EC2                 # per-service overrides
              value: "https://wiremock.wiremock-system.svc"
            - name: AWS_ENDPOINT_URL_ELB
              value: "https://wiremock.wiremock-system.svc"
            - name: AWS_ENDPOINT_URL_STS
              value: "https://wiremock.wiremock-system.svc"
            - name: AWS_ENDPOINT_URL_IAM
              value: "https://wiremock.wiremock-system.svc"
            - name: AWS_ENDPOINT_URL_SSM
              value: "https://wiremock.wiremock-system.svc"
            - name: AWS_ENDPOINT_URL_SECRETSMANAGER
              value: "https://wiremock.wiremock-system.svc"
            - name: AWS_ENDPOINT_URL_EKS
              value: "https://wiremock.wiremock-system.svc"
            - name: SSL_CERT_FILE                        # Go crypto/x509 trust root
              value: /certs/ca.crt
          volumeMounts:
            - name: wiremock-ca
              mountPath: /certs
              readOnly: true
```

ACK S3, `deployment/ack-s3-controller-s3-chart` in `ack-system`, container
`controller`, same pattern with `AWS_ENDPOINT_URL`, `AWS_ENDPOINT_URL_S3`,
`AWS_ENDPOINT_URL_STS`, `SSL_CERT_FILE`, and the same CA mount. Chart
installed with the repo's value shape (`aws.region: eu-north-1`,
`aws.credentials.secretName: aws-credentials`, `secretKey: credentials`,
`profile: default`), the credentials Secret holding the dummy pair.

Two notes on trust and TLS:

- `SSL_CERT_FILE` is honored by Go's `crypto/x509` (`SystemCertPool`), which
  the AWS SDK Go v2 default HTTP client uses. The controller images are
  distroless (no CA bundle, no shell), so the env var plus a mounted file is
  the working lever. `AWS_CA_BUNDLE` is also honored by AWS SDK Go v2 (the
  config package reads it into `CustomCABundle`); `SSL_CERT_FILE` was used
  here because it needs no SDK involvement and applies to any Go binary.
- WireMock 3.13.2's `--key-manager-password` defaults to the literal string
  `password` independently of `--keystore-password`. A keystore whose key
  entry password differs from that default fails at boot with
  `UnrecoverableKeyException`. Set both flags (or use `password` for both).
  A LibreSSL-produced PKCS12 (macOS openssl) also failed to load; building
  the JKS natively with the image's own keytool (genkeypair, certreq, CA
  sign, import chain) is the reliable path.

## Results per controller

### CAPA v2.13.0: endpoint override honored, no caveats

The `AWSCluster` reconcile called EC2 `DescribeVpcs` against WireMock within
seconds of the CR landing. The controller logged the expected unmatched-stub
error (WireMock 404 surfaced by the SDK as `UnknownError`):

```
failed to describe VPC resources by name: failed to query ec2 for VPCs by name
"spike-vpc": operation error EC2: DescribeVpcs, https response error StatusCode: 404
```

A TLS failure would have said `x509: certificate signed by unknown authority`
and a DNS failure `no such host`; a clean HTTP 404 from the EC2 client proves
the full chain (env override, cluster DNS, CA trust, WireMock arrival).
WireMock logged 14 identical calls in the first window.

Redacted journal sample (`/__admin/requests`):

```json
{
  "method": "POST",
  "url": "/",
  "absoluteUrl": "https://wiremock.wiremock-system.svc/",
  "protocol": "HTTP/2.0",
  "headers": {
    "user-agent": "aws-sdk-go-v2/1.41.5 ua/2.1 os/linux lang/go#1.25.12 ... api/ec2#1.288.0 aws.cluster.x-k8s.io/v2.13.0 m/E,n",
    "authorization": "AWS4-HMAC-SHA256 Credential=AKIAIO...MPLE/20260920/eu-north-1/ec2/aws4_request, SignedHeaders=...",
    "content-type": "application/x-www-form-urlencoded"
  },
  "body": "Action=DescribeVpcs&Filter.1.Name=tag%3AName&Filter.1.Value.1=spike-vpc&Version=2016-11-15"
}
```

The SigV4 credential scope (`eu-north-1/ec2`) and the query body confirm the
request is CAPA's real reconcile traffic, redirected only at the endpoint
level. The global override alone was sufficient; the per-service variables
were set as well but not separately required for EC2.

### ACK S3 chart 1.11.0: endpoint override honored, two caveats

Caveat 1, startup STS dependency. Before patching, the controller crash
looped at boot:

```
Unable to create controller manager ... unable to get caller identity:
operation error STS: GetCallerIdentity, https response error StatusCode: 403,
api error InvalidClientTokenId: The security token included in the request is invalid.
```

Two facts in that line: ACK S3 calls STS `GetCallerIdentity` at startup and
treats failure as fatal, and unpatched it placed that call to the real
`sts.amazonaws.com` (real AWS RequestID in the error). For the virtualized
environment this means the WireMock stub set must include an STS
`GetCallerIdentity` response from the start, even in Phase 1 before any
recordings exist. With a static stub (account `000000000000`, dummy ARN) the
controller booted cleanly and started its Bucket workers.

Caveat 2, S3 virtual-hosted addressing. Bucket-less operations (`ListBuckets`)
went straight to the override host and arrived immediately:

```
GET https://wiremock.wiremock-system.svc/?x-id=ListBuckets
user-agent: aws-controllers-k8s/s3.services.k8s.aws-1.11.0 ... api/s3#1.97.3
authorization: AWS4-HMAC-SHA256 Credential=AKIAIO...MPLE/.../eu-north-1/s3/aws4_request
```

Bucket-scoped operations are different: the SDK composes the request URL as
`<bucket>.<endpoint-host>` (virtual-hosted style; AWS SDK Go v2 has no env or
shared-config switch for path style). `CreateBucket` targeted
`https://krops-wiremock-spike-dummy-f11dc82d.wiremock.wiremock-system.svc/`
and failed client-side in two successive ways, each fixed without touching
real AWS hostnames:

1. DNS: `no such host` for the derived name. Fixed with one CoreDNS
   `template` stanza resolving `*.<wiremock service FQDN>` to the Service
   ClusterIP:

   ```
   template IN A wiremock-system.svc.cluster.local {
       match .*\.wiremock\.wiremock-system\.svc\.cluster\.local\.
       answer "{{ .Name }} 60 IN A <wiremock ClusterIP>"
       fallthrough
   }
   ```

2. TLS: `x509: certificate is valid for ... not
   krops-wiremock-spike-dummy-f11dc82d.wiremock.wiremock-system.svc`. The SDK
   dials the unqualified host form (the resolver expands it for DNS, but TLS
   verification uses the original name), so the wildcard SAN must cover
   `*.wiremock.wiremock-system.svc`, not only
   `*.wiremock.wiremock-system.svc.cluster.local`. Rebuilt the server
   certificate with both wildcard forms.

With both in place, `CreateBucket` arrived at WireMock (13 logged calls):

```json
{
  "method": "PUT",
  "absoluteUrl": "https://krops-wiremock-spike-dummy-f11dc82d.wiremock.wiremock-system.svc/",
  "authorization": "AWS4-HMAC-SHA256 Credential=AKIAIO...MPLE/20260920/eu-north-1/s3/aws4_request, ...",
  "user-agent": "aws-controllers-k8s/s3.services.k8s.aws-1.11.0 (GitCommit/6c4afaf...; CRDKind/Bucket; CRDVersion/v1alpha1) aws-sdk-go-v2/1.41.5 ...",
  "response_status": 404
}
```

The 404 is WireMock's unmatched-request page (HTML), which the S3 XML parser
reports as a deserialization error. Expected: unmatched responses are not
AWS-shaped until Phase 2 recordings provide real stubs. Arrival is what this
spike needed to prove.

## Decision gate outcome

| Controller | `AWS_ENDPOINT_URL` honored | Extra machinery needed |
|---|---|---|
| CAPA v2.13.0 | yes | none |
| ACK S3 1.11.0 | yes | STS `GetCallerIdentity` stub at boot; CoreDNS template + wildcard SAN (unqualified form) for S3 virtual-hosted bucket names |

For the AWS cloud the Phase 0 gate passes: no network-layer interception
(HTTPS proxy or CoreDNS rewrite of `*.amazonaws.com`) is required. The
CoreDNS stanza above only resolves a derived in-cluster name; no real AWS
hostname is ever rewritten.

## Implications for Phase 1

- WireMock deployment: throwaway CA, JKS built with the image's own keytool,
  `--keystore-password` and `--key-manager-password` set explicitly, HTTPS on
  8443 behind a ClusterIP Service on 443.
- Server certificate SANs: the Service names in short and FQDN form, plus
  wildcards in both forms (`*.wiremock.wiremock-system.svc` and
  `*.wiremock.wiremock-system.svc.cluster.local`) for S3.
- Controller patches: `AWS_ENDPOINT_URL` (global covers every service tested;
  per-service variables work too if Phase 2 wants to split recordings per
  service), `SSL_CERT_FILE=/certs/ca.crt`, CA ConfigMap per consuming
  namespace.
- ACK bootstrap stub: a static STS `GetCallerIdentity` mapping is required
  before any ACK controller will start its workers.
- S3 DNS: ship the CoreDNS template stanza in the AWS harness when the Bucket
  CRD is in scope; harmless otherwise.
- Unmatched-request handling: controllers surface WireMock's unmatched 404 as
  generic SDK errors and keep requeuing, which is fine for arrival checks and
  is exactly what the Phase 5 `/__admin/requests/unmatched` assertion rides
  on.

## Environment caveats observed during the spike

- On a host running several kind clusters, the scratch cluster's API server
  was slow enough that CAPA's leader election (5s lease timeout against the
  API) flapped and the manager restarted a few times. Arrivals were still
  confirmed once a stable leader held the lease. CI runners with a single
  kind cluster should not see this; if they do, raise the controller's
  leader-election timeouts for the test namespace, not the endpoint wiring.
- helm `--wait` on the initial ACK install reported failure because the
  unpatched controller crash loops on real-STS auth; the Deployment itself is
  fine and becomes ready after the patch. The Phase 1 script should patch
  before waiting, or wait with `--timeout` tolerance for this boot race.

## Cleanup

`kind delete cluster --name wiremock-aws-spike` after the runs. No AWS
resources exist to clean (dummy credentials never authenticated anywhere;
the only real AWS call ever placed was the unpatched ACK startup STS probe,
which was rejected with `InvalidClientTokenId`). No local clusters left
running by this spike.
