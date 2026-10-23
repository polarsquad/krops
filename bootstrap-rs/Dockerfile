# krops toolbox image (issue #104).
#
# Ships krops-bootstrap plus the full pinned tool layer so a host needs only
# a container engine to run the whole bootstrap lifecycle. Tool versions
# are single-sourced from the committed mise configs (Renovate-managed
# there, never duplicated into this file); the CLI is compiled with the
# toolchain pinned in bootstrap-rs/rust-toolchain.toml.
#
# Build (from the repository root; the context is the repo root):
#   docker build -f bootstrap-rs/Dockerfile -t krops-toolbox:dev .
#
# Base image digests and the mise CLI pin are Renovate-managed
# (dockerfile manager with pinDigests, and the jdx/mise custom manager
# extended to this file's ARG MISE_VERSION line).

# ── Stage 1: compile krops-bootstrap with the pinned toolchain ──────────────
# slim-bookworm matches the runtime stage's glibc (bookworm, 2.36): a
# binary built on the default rust image (trixie, glibc 2.39) fails on
# bookworm with "GLIBC_2.39 not found".
FROM rust:1.99.1-slim-bookworm@sha256:a24e292b4af7bdee351a31d3a6379972a5652f0ea22a1b6d6883324ac4f5097c AS build

WORKDIR /build
COPY bootstrap-rs/ ./bootstrap-rs/
RUN cd bootstrap-rs && cargo build --locked --release \
    && cp target/release/krops-bootstrap /usr/local/bin/krops-bootstrap

# ── Stage 2: runtime with the pinned tool layer ───────────────────────────
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

# renovate: datasource=github-releases depName=jdx/mise extractVersion=^v(?<version>.+) versioning=semver
ARG MISE_VERSION=2026.10.17
# Set by BuildKit: "amd64" or "arm64".
ARG TARGETARCH

# docker-ce-cli (client only) comes from Docker's apt repo: bookworm has no
# standalone docker-cli package, and docker.io would drag in the whole
# engine. The client floats with the repo (client/server API is backward
# compatible; the daemon always runs on the host). The podman remote client
# (static build) provides the podman provider path kind and krops-bootstrap
# exec (CONTAINER_ENGINE=podman); it talks to the host socket the run
# invocation mounts.
# renovate: datasource=github-releases depName=containers/podman extractVersion=^v(?<version>.+) versioning=semver
ARG PODMAN_VERSION=6.1.4
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq findutils gnupg python-is-python3 \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
         -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH}" in \
         amd64) MISE_ASSET=mise-v${MISE_VERSION}-linux-x64 ;; \
         arm64) MISE_ASSET=mise-v${MISE_VERSION}-linux-arm64 ;; \
         *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/${MISE_ASSET}" \
         -o /usr/local/bin/mise \
    && chmod +x /usr/local/bin/mise \
    && curl -fsSL "https://github.com/containers/podman/releases/download/v${PODMAN_VERSION}/podman-remote-static-linux_${TARGETARCH}.tar.gz" \
         | tar -xz -C /tmp --strip-components=1 "bin/podman-remote-static-linux_${TARGETARCH}" \
    && mv "/tmp/podman-remote-static-linux_${TARGETARCH}" /usr/local/bin/podman \
    && chmod +x /usr/local/bin/podman

# The repository is mounted at /workspace at run time; mise runs from the
# working directory with both env layers available (aws tools per the #104
# decision: one image, credentials arrive at runtime). Auto-install is off
# (#601): the image carries the full pinned tool layer, so a lifecycle task
# that touches a dev-only pin (go, zarf) must fail loudly on the missing
# tool instead of downloading it mid-run. An operator can opt back in per
# run with -e MISE_AUTO_INSTALL=1.
ENV MISE_DATA_DIR=/usr/local/share/mise \
    MISE_YES=1 \
    MISE_AUTO_INSTALL=0 \
    AWS_PAGER="" \
    PATH=/usr/local/share/mise/shims:/usr/local/bin:/usr/bin:/sbin:/bin

COPY --from=build /usr/local/bin/krops-bootstrap /usr/local/bin/krops-bootstrap
COPY bootstrap-rs/toolbox-entrypoint.sh /usr/local/bin/toolbox-entrypoint.sh
COPY mise.toml mise.aws.toml mise.azure.toml mise.gcp.toml /opt/krops/

RUN chmod +x /usr/local/bin/toolbox-entrypoint.sh

# Install every tool the configs pin except the dev-only toolchains the
# lifecycle never invokes (go, python, zarf: airgap builds run on the
# host, not inside the toolbox). The aws layer (aws-cli, clusterawsadm)
# and the azure layer (azure-cli, for the arc-federate hook and
# azure-bootstrap) install too; credentials arrive at runtime. azure-cli
# is a pipx backend tool and needs the pinned python present to install.
# The gcp layer (gcloud, for the wif-federate hook and gcp-bootstrap)
# installs after the python layer: on arm64 the tarball ships no bundled
# python, so the installer's provisioning probe needs a bare `python`
# (the apt python-is-python3 above plus the pinned python 3.14.7 here).
# gke-gcloud-auth-plugin is not in the SDK tarball, so it is added as a
# Cloud SDK component before the shim is generated. `mise x` is avoided here:
# it auto-installs every tool in the env (including the dev-only zarf) and a
# zarf auto-install failure would abort the build. `mise which gcloud`
# resolves the path without any install. After the aws tools install,
# `mise use -g` bakes their exact pins into the image's global mise config
# (the versionless form would write "latest", which could make a bare shim
# fetch a newer release at run time). The pins stay single-sourced: they
# are read back from the aws env layer's resolution, so the checkout
# mounted at /workspace at run time needs no env layer for a bare `aws` or
# `clusterawsadm` shim to resolve (issue #635). The smoke RUN below fails
# the build if that resolution regresses.
RUN cd /opt/krops \
    && mise install -y kind helm kubectl clusterctl flux2 sops age \
    && mise -E aws install -y aws-cli clusterawsadm \
    && mise -E aws use -g "aws-cli@$(mise -E aws current aws-cli)" "clusterawsadm@$(mise -E aws current clusterawsadm)" \
    && mise install -y python uv \
    && mise -E azure install -y azure-cli \
    && mise -E gcp install -y gcloud \
    && "$(mise -E gcp which gcloud)" components install gke-gcloud-auth-plugin --quiet \
    && mise reshim

# Build-time smoke check for the global aws bindings above (#635): a bare
# shim invocation must resolve with no env layer active. If shim resolution
# regresses, the build fails here instead of mid-bootstrap.
RUN aws --version && clusterawsadm version

# The container runs as root while the bind-mounted checkout is owned by the
# host user, so git refuses to read it on native rootful Linux Docker
# ("detected dubious ownership", issue #485). Docker Desktop and rootless
# engines map the mount to the container user and never hit it. /workspace is
# the only mount that is a git repository (scripts/toolbox-run.sh), so trust
# that path and nothing else; --system keeps it protected configuration that
# the mounted repository cannot override.
RUN git config --system --add safe.directory /workspace

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/toolbox-entrypoint.sh"]
