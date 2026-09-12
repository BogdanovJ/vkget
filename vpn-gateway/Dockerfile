FROM alpine:3.21

RUN apk add --no-cache openvpn python3 \
    && apk add --no-cache -X https://dl-cdn.alpinelinux.org/alpine/v3.21/community microsocks

WORKDIR /app
COPY ovpn.py server.py entrypoint.sh /app/
RUN chmod 0755 /app/entrypoint.sh

EXPOSE 8081 1080
CMD ["/app/entrypoint.sh"]
