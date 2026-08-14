FROM rust:1-bookworm AS builder

WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY tests ./tests
RUN cargo build --release --locked

FROM debian:bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 testagent

COPY --from=builder /build/target/release/web-capability /usr/local/bin/web-capability

ENV WEB_CAPABILITY_BIND=0.0.0.0:3020
USER testagent
EXPOSE 3020

CMD ["web-capability"]
