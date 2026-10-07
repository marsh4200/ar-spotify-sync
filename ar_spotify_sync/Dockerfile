# AR Spotify Sync
#
# Debian based on purpose: the AirPlay sender binary is built against glibc.
# Both third-party binaries are downloaded from their official GitHub releases
# at pinned versions and verified against pinned SHA-256 checksums.
FROM python:3.13-slim-bookworm

ARG CLIAIRPLAY_VERSION=v0.5.5
ARG CLIAIRPLAY_SHA256_X86_64=d5302f0b9f24fd8a045a75bf919c1345af38c88487d817928ddb91edc475af07
ARG CLIAIRPLAY_SHA256_AARCH64=57f362ab09c01e069f6691abcef193bb5e49ff8146feb697881e166a153a6c01

ARG GO_LIBRESPOT_VERSION=0.9.0
ARG GO_LIBRESPOT_SHA256_X86_64=87b27ce57cc7871bad6ffac8acba7e2a89c72a7e6c7990b88bec404f449f381e
ARG GO_LIBRESPOT_SHA256_ARM64=79b80bb3723b7973165d2d94c428676b8582780aeca7c54694589206ab741e91

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl libasound2; \
    case "$(uname -m)" in \
        x86_64) \
            CLI_ARCH=x86_64; CLI_SHA="${CLIAIRPLAY_SHA256_X86_64}"; \
            GL_ARCH=x86_64; GL_SHA="${GO_LIBRESPOT_SHA256_X86_64}" ;; \
        aarch64) \
            CLI_ARCH=aarch64; CLI_SHA="${CLIAIRPLAY_SHA256_AARCH64}"; \
            GL_ARCH=arm64; GL_SHA="${GO_LIBRESPOT_SHA256_ARM64}" ;; \
        *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/cliairplay \
        "https://github.com/music-assistant/airplay-cli/releases/download/${CLIAIRPLAY_VERSION}/cliairplay-linux-${CLI_ARCH}"; \
    echo "${CLI_SHA}  /usr/local/bin/cliairplay" | sha256sum -c -; \
    chmod 755 /usr/local/bin/cliairplay; \
    /usr/local/bin/cliairplay --check; \
    curl -fsSL -o /tmp/go-librespot.tar.gz \
        "https://github.com/devgianlu/go-librespot/releases/download/v${GO_LIBRESPOT_VERSION}/go-librespot_linux_${GL_ARCH}.tar.gz"; \
    echo "${GL_SHA}  /tmp/go-librespot.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/go-librespot.tar.gz -C /usr/local/bin go-librespot; \
    chmod 755 /usr/local/bin/go-librespot; \
    rm /tmp/go-librespot.tar.gz; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

RUN pip install aiohttp==3.14.4 zeroconf==0.151.5

COPY app /app

CMD ["python3", "-u", "/app/main.py"]
