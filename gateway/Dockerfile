FROM kong:3.9
COPY kong.yml.template /etc/kong/kong.yml.template
COPY --chmod=755 entrypoint.sh /usr/local/bin/gateway-entrypoint.sh
ENV KONG_DATABASE=off \
    KONG_PROXY_LISTEN="0.0.0.0:8000" \
    KONG_ADMIN_LISTEN="off" \
    KONG_STATUS_LISTEN="0.0.0.0:8100" \
    KONG_PROXY_ACCESS_LOG=/dev/stdout \
    KONG_PROXY_ERROR_LOG=/dev/stderr \
    KONG_UNTRUSTED_LUA=sandbox \
    KONG_PLUGINS=bundled
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/gateway-entrypoint.sh"]
