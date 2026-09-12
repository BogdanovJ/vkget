FROM alpine:3.21

# microsocks is not packaged for Alpine 3.21 aarch64 (only edge/testing x86_64),
# so build the small upstream binary instead of depending on apk.
ARG MICROSOCKS_VERSION=1.0.5

RUN apk add --no-cache \
        openvpn \
        python3 \
        wireguard-tools \
        iproute2 \
        iptables \
        build-base \
    && wget -qO- "https://github.com/rofl0r/microsocks/archive/refs/tags/v${MICROSOCKS_VERSION}.tar.gz" \
        | tar -xz -C /tmp \
    && make -C "/tmp/microsocks-${MICROSOCKS_VERSION}" \
    && install -m 0755 "/tmp/microsocks-${MICROSOCKS_VERSION}/microsocks" /usr/local/bin/microsocks \
    && rm -rf "/tmp/microsocks-${MICROSOCKS_VERSION}" \
    && apk del build-base

WORKDIR /app
COPY ovpn.py wg.py server.py entrypoint.sh /app/
RUN chmod 0755 /app/entrypoint.sh

EXPOSE 8081 1080
CMD ["/app/entrypoint.sh"]
