#!/bin/sh
# Render the declarative config from environment variables, then start Kong (DB-less).
set -eu
: "${API_UPSTREAM:?API_UPSTREAM is required, e.g. http://api:8000}"
: "${JWT_SECRET:?JWT_SECRET is required}"
JWT_ISSUER="${JWT_ISSUER:-tuition-auth}"
MOBILE_DAILY_LIMIT="${MOBILE_DAILY_LIMIT:-3}"
sed -e "s|\${API_UPSTREAM}|${API_UPSTREAM}|g" \
    -e "s|\${JWT_SECRET}|${JWT_SECRET}|g" \
    -e "s|\${JWT_ISSUER}|${JWT_ISSUER}|g" \
    -e "s|\${MOBILE_DAILY_LIMIT}|${MOBILE_DAILY_LIMIT}|g" \
    /etc/kong/kong.yml.template > /tmp/kong.yml
export KONG_DECLARATIVE_CONFIG=/tmp/kong.yml
exec /docker-entrypoint.sh kong docker-start
