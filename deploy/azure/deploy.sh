#!/usr/bin/env bash
# Deploys the whole system to Azure (works in Azure Cloud Shell - nothing to install locally):
#
#   Azure Database for PostgreSQL Flexible Server  (server database)
#   Azure Container Registry                       (images are built IN Azure with `az acr build`)
#   Azure Container Apps environment with 4 apps:
#     tuition-api   internal ingress only  -> reachable only through the gateway
#     api-gateway   external (Kong)        -> public REST entry point, /api/v1/...
#     mcp-server    external               -> MCP Streamable HTTP endpoint /mcp
#     tuition-agent external               -> chat UI
#
# Usage (Cloud Shell, Bash):
#   git clone https://github.com/<you>/<repo>.git && cd <repo>
#   export LLM_API_KEY=<your Gemini key>
#   LOCATION=swedencentral bash deploy/azure/deploy.sh
#
# Azure for Students subscriptions only allow some regions. If a step fails with
# "RequestDisallowedByAzure" / "location not allowed", run again with another LOCATION
# (e.g. polandcentral, germanywestcentral, northeurope, francecentral, uksouth).
set -euo pipefail

: "${LLM_API_KEY:?Set LLM_API_KEY first (export LLM_API_KEY=...)}"
LOCATION="${LOCATION:-swedencentral}"
RG="${RG:-rg-tuition-assistant}"
OUT="$(dirname "$0")/outputs.env"
# Re-use generated names/secrets on re-runs
[ -f "$OUT" ] && source "$OUT"
SUFFIX="${SUFFIX:-$(tr -dc a-z0-9 </dev/urandom | head -c 6)}"
ACR="${ACR:-acrtuition${SUFFIX}}"
PG="${PG:-pg-tuition-${SUFFIX}}"
CAE="${CAE:-cae-tuition}"
PG_ADMIN="${PG_ADMIN:-tuitionadmin}"
PG_PASS="${PG_PASS:-Tu$(tr -dc A-Za-z0-9 </dev/urandom | head -c 20)9}"
JWT_SECRET="${JWT_SECRET:-$(tr -dc a-f0-9 </dev/urandom | head -c 64)}"
LLM_MODEL="${LLM_MODEL:-gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3.5-flash}"
LLM_BASE_URL="${LLM_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai/}"
TAG="${TAG:-v$(date +%Y%m%d%H%M)}"

cat > "$OUT" <<EOF
SUFFIX=$SUFFIX
ACR=$ACR
PG=$PG
CAE=$CAE
PG_ADMIN=$PG_ADMIN
PG_PASS=$PG_PASS
JWT_SECRET=$JWT_SECRET
EOF
echo ">> names/secrets saved to $OUT (do NOT commit this file)"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
set -f  # no globbing: secrets/URLs contain ? and &
step() { printf '\n\033[1;34m>> %s\033[0m\n' "$*"; }

step "Providers and CLI extension"
az extension add --name containerapp --upgrade -y --only-show-errors
for p in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.DBforPostgreSQL; do
  az provider register -n "$p" --wait --only-show-errors
done

step "Resource group $RG ($LOCATION)"
az group create -n "$RG" -l "$LOCATION" -o none

step "Container registry $ACR"
az acr show -n "$ACR" -o none 2>/dev/null || az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled true -o none
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)
ACR_USER=$(az acr credential show -n "$ACR" --query username -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv)

step "Building images in Azure (az acr build) - tag $TAG"
az acr build -r "$ACR" -t "tuition-api:$TAG"   "$ROOT/tuition_api" --only-show-errors -o none
az acr build -r "$ACR" -t "api-gateway:$TAG"   "$ROOT/gateway"     --only-show-errors -o none
az acr build -r "$ACR" -t "mcp-server:$TAG"    "$ROOT/mcp_server"  --only-show-errors -o none
az acr build -r "$ACR" -t "tuition-agent:$TAG" "$ROOT/agent"       --only-show-errors -o none

step "PostgreSQL Flexible Server $PG (Burstable B1ms, takes ~5-10 min the first time)"
if ! az postgres flexible-server show -g "$RG" -n "$PG" -o none 2>/dev/null; then
  az postgres flexible-server create -g "$RG" -n "$PG" -l "$LOCATION" \
    --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
    --admin-user "$PG_ADMIN" --admin-password "$PG_PASS" \
    --public-access 0.0.0.0 --yes -o none   # 0.0.0.0 = allow access from Azure services only
fi
az postgres flexible-server db create -g "$RG" -s "$PG" -d tuition -o none 2>/dev/null || true
DATABASE_URL="postgresql+psycopg://${PG_ADMIN}:${PG_PASS}@${PG}.postgres.database.azure.com:5432/tuition?sslmode=require"

step "Container Apps environment $CAE"
az containerapp env show -g "$RG" -n "$CAE" -o none 2>/dev/null || \
  az containerapp env create -g "$RG" -n "$CAE" -l "$LOCATION" -o none

# create-or-update helper
deploy_app() {  # name image port ingress secrets env...
  local name=$1 image=$2 port=$3 ingress=$4 secrets=$5; shift 5
  if az containerapp show -g "$RG" -n "$name" -o none 2>/dev/null; then
    [ -n "$secrets" ] && az containerapp secret set -g "$RG" -n "$name" --secrets $secrets -o none
    az containerapp update -g "$RG" -n "$name" --image "$ACR_SERVER/$image" --set-env-vars "$@" -o none
  else
    az containerapp create -g "$RG" -n "$name" --environment "$CAE" \
      --image "$ACR_SERVER/$image" --registry-server "$ACR_SERVER" \
      --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
      --target-port "$port" --ingress "$ingress" \
      --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
      ${secrets:+--secrets $secrets} --env-vars "$@" -o none
  fi
  az containerapp show -g "$RG" -n "$name" --query properties.configuration.ingress.fqdn -o tsv
}

step "tuition-api (internal ingress)"
API_FQDN=$(deploy_app tuition-api "tuition-api:$TAG" 8000 internal \
  "db-url=$DATABASE_URL jwt-secret=$JWT_SECRET" \
  DATABASE_URL=secretref:db-url JWT_SECRET=secretref:jwt-secret JWT_ISSUER=tuition-auth MOBILE_DAILY_LIMIT=3)

step "api-gateway (Kong, external)"
GW_FQDN=$(deploy_app api-gateway "api-gateway:$TAG" 8000 external \
  "jwt-secret=$JWT_SECRET" \
  API_UPSTREAM="https://$API_FQDN" JWT_SECRET=secretref:jwt-secret JWT_ISSUER=tuition-auth MOBILE_DAILY_LIMIT=3)

step "mcp-server (external)"
MCP_FQDN=$(deploy_app mcp-server "mcp-server:$TAG" 8001 external "" PORT=8001)

step "tuition-agent (chat UI, external)"
AGENT_FQDN=$(deploy_app tuition-agent "tuition-agent:$TAG" 8080 external \
  "llm-key=$LLM_API_KEY" \
  GATEWAY_URL="https://$GW_FQDN" MCP_URL="https://$MCP_FQDN/mcp" \
  LLM_API_KEY=secretref:llm-key LLM_MODEL="$LLM_MODEL" LLM_BASE_URL="$LLM_BASE_URL")

cat >> "$OUT" <<EOF
GATEWAY_URL=https://$GW_FQDN
MCP_URL=https://$MCP_FQDN/mcp
CHAT_URL=https://$AGENT_FQDN
API_INTERNAL_FQDN=$API_FQDN
EOF

step "Done"
cat <<EOF
  API Gateway (all REST):  https://$GW_FQDN/api/v1/...
  Swagger UI:              https://$GW_FQDN/docs
  MCP server:              https://$MCP_FQDN/mcp
  Chat UI:                 https://$AGENT_FQDN
  Tuition API (internal):  https://$API_FQDN   (not reachable from the internet)

Smoke test:   python3 scripts/smoke_test.py https://$GW_FQDN
Delete all:   az group delete -n $RG --yes --no-wait
EOF
