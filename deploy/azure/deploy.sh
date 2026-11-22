#!/usr/bin/env bash
# Deploys the whole system to Azure (works in Azure Cloud Shell - nothing to install locally):
#
#   Azure Database for PostgreSQL Flexible Server  (server database)
#   Azure Container Registry                       (images: `az acr build`, or pushed from your PC with Docker
#                                                   when ACR Tasks are blocked, e.g. on Azure for Students)
#   Azure Container Apps environment with 4 apps:
#     tuition-api   ingress on, but every request without the gateway's shared secret is rejected (403)
#                   -> in practice reachable only through the gateway
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
#
# The script is safe to re-run: it re-uses the resources that already exist in the resource group
# (also when Cloud Shell lost deploy/azure/outputs.env).
set -euo pipefail

: "${LLM_API_KEY:?Set LLM_API_KEY first (export LLM_API_KEY=...)}"
LOCATION="${LOCATION:-swedencentral}"
RG="${RG:-rg-tuition-assistant}"
OUT="$(dirname "$0")/outputs.env"
# Re-use generated names/secrets on re-runs
[ -f "$OUT" ] && source "$OUT"
# Cloud Shell without storage may have lost outputs.env: discover existing resources in the group
if [ -z "${ACR:-}" ] && az group show -n "$RG" -o none 2>/dev/null; then
  ACR=$(az acr list -g "$RG" --query "[0].name" -o tsv 2>/dev/null || true); [ -z "$ACR" ] && unset ACR
  PG=$(az postgres flexible-server list -g "$RG" --query "[0].name" -o tsv 2>/dev/null || true); [ -z "$PG" ] && unset PG
fi
SUFFIX="${SUFFIX:-$(openssl rand -hex 3)}"
ACR="${ACR:-acrtuition${SUFFIX}}"
PG="${PG:-pg-tuition-${SUFFIX}}"
CAE="${CAE:-cae-tuition}"
PG_ADMIN="${PG_ADMIN:-tuitionadmin}"
PG_PASS="${PG_PASS:-Tu$(openssl rand -hex 10)9}"
JWT_SECRET="${JWT_SECRET:-$(openssl rand -hex 32)}"
GATEWAY_SHARED_SECRET="${GATEWAY_SHARED_SECRET:-$(openssl rand -hex 24)}"
LLM_MODEL="${LLM_MODEL:-gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3.5-flash}"
LLM_BASE_URL="${LLM_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai/}"
# Image tag. Keep "v1" for the first deployment; use TAG=v2 (and push v2) when you redeploy changed code.
TAG="${TAG:-v1}"

cat > "$OUT" <<EOF
SUFFIX=$SUFFIX
ACR=$ACR
PG=$PG
CAE=$CAE
PG_ADMIN=$PG_ADMIN
PG_PASS=$PG_PASS
JWT_SECRET=$JWT_SECRET
GATEWAY_SHARED_SECRET=$GATEWAY_SHARED_SECRET
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

step "PostgreSQL Flexible Server $PG (Burstable B1ms, takes ~5-10 min the first time)"
if ! az postgres flexible-server show -g "$RG" -n "$PG" -o none 2>/dev/null; then
  az postgres flexible-server create -g "$RG" -n "$PG" -l "$LOCATION" \
    --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
    --admin-user "$PG_ADMIN" --admin-password "$PG_PASS" \
    --public-access 0.0.0.0 --yes -o none   # 0.0.0.0 = allow access from Azure services only
else
  # existing server: make sure the password we pass to the API is the current one
  az postgres flexible-server update -g "$RG" -n "$PG" --admin-password "$PG_PASS" -o none
fi
# the application database (newer CLI versions use --server-name/--name; retry while the server is busy)
if ! az postgres flexible-server db show -g "$RG" --server-name "$PG" --database-name tuition -o none 2>/dev/null; then
  for i in 1 2 3 4 5; do
    az postgres flexible-server db create -g "$RG" --server-name "$PG" --name tuition -o none && break
    [ "$i" = 5 ] && { echo "Could not create database 'tuition'"; exit 1; }
    echo "   database not created yet, retrying in 30 s ..."; sleep 30
  done
fi
DATABASE_URL="postgresql+psycopg://${PG_ADMIN}:${PG_PASS}@${PG}.postgres.database.azure.com:5432/tuition?sslmode=require"

step "Container Apps environment $CAE"
az containerapp env show -g "$RG" -n "$CAE" -o none 2>/dev/null || \
  az containerapp env create -g "$RG" -n "$CAE" -l "$LOCATION" -o none

step "Container images (tag $TAG)"
IMAGES="tuition-api:tuition_api api-gateway:gateway mcp-server:mcp_server tuition-agent:agent"
image_exists() { az acr repository show-tags -n "$ACR" --repository "$1" -o tsv 2>/dev/null | grep -qx "$TAG"; }
missing=""
for pair in $IMAGES; do
  repo=${pair%%:*}; dir=${pair##*:}
  if image_exists "$repo"; then echo "   $repo:$TAG already in $ACR_SERVER"; continue; fi
  echo "   building $repo:$TAG with ACR Tasks ..."
  if ! az acr build -r "$ACR" -t "$repo:$TAG" "$ROOT/$dir" --only-show-errors -o none 2>/tmp/acr_build.err; then
    if grep -q "TasksOperationsNotAllowed" /tmp/acr_build.err; then
      missing="$missing $repo"
    else
      cat /tmp/acr_build.err; exit 1
    fi
  fi
done
if [ -n "$missing" ]; then
  printf '\n\033[1;33m!! ACR Tasks (az acr build) are not allowed on this subscription (normal for Azure for Students).\033[0m\n'
  cat <<EOF
Push the images from YOUR computer with Docker instead. In the project folder, in PowerShell, run:

docker compose build api gateway mcp agent
docker login $ACR_SERVER -u $ACR_USER -p "$ACR_PASS"
docker tag tuition-assistant-api     $ACR_SERVER/tuition-api:$TAG
docker tag tuition-assistant-gateway $ACR_SERVER/api-gateway:$TAG
docker tag tuition-assistant-mcp     $ACR_SERVER/mcp-server:$TAG
docker tag tuition-assistant-agent   $ACR_SERVER/tuition-agent:$TAG
docker push $ACR_SERVER/tuition-api:$TAG
docker push $ACR_SERVER/api-gateway:$TAG
docker push $ACR_SERVER/mcp-server:$TAG
docker push $ACR_SERVER/tuition-agent:$TAG

Then run this script again (same command). Everything created so far is re-used.
EOF
  exit 2
fi

# create-or-update helper
deploy_app() {  # name image port ingress secrets env...
  local name=$1 image=$2 port=$3 ingress=$4 secrets=$5; shift 5
  if az containerapp show -g "$RG" -n "$name" -o none 2>/dev/null; then
    [ -n "$secrets" ] && az containerapp secret set -g "$RG" -n "$name" --secrets $secrets -o none
    az containerapp update -g "$RG" -n "$name" --image "$ACR_SERVER/$image" \
      --set-env-vars "$@" DEPLOYED_AT="$(date +%s)" -o none
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

# Note: internal-only ingress was not reachable from the other apps in this environment type (404 / DNS
# errors from Kong), so the API uses normal ingress and enforces "gateway only" with a shared secret that
# only Kong adds (X-Gateway-Secret). Direct calls get 403 GATEWAY_REQUIRED.
step "tuition-api (accepts only requests that carry the gateway secret)"
API_FQDN=$(deploy_app tuition-api "tuition-api:$TAG" 8000 external \
  "db-url=$DATABASE_URL jwt-secret=$JWT_SECRET gateway-secret=$GATEWAY_SHARED_SECRET" \
  DATABASE_URL=secretref:db-url JWT_SECRET=secretref:jwt-secret JWT_ISSUER=tuition-auth MOBILE_DAILY_LIMIT=3 \
  GATEWAY_SHARED_SECRET=secretref:gateway-secret)

step "api-gateway (Kong, external)"
GW_FQDN=$(deploy_app api-gateway "api-gateway:$TAG" 8000 external \
  "jwt-secret=$JWT_SECRET gateway-secret=$GATEWAY_SHARED_SECRET" \
  API_UPSTREAM="https://$API_FQDN" JWT_SECRET=secretref:jwt-secret JWT_ISSUER=tuition-auth MOBILE_DAILY_LIMIT=3 \
  GATEWAY_SHARED_SECRET=secretref:gateway-secret)

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
  Tuition API:             https://$API_FQDN   (direct calls -> 403 GATEWAY_REQUIRED)

Evidence (run on your computer, in the project folder):
  docker compose run --rm -v ./scripts:/scripts -v ./samples:/samples agent python /scripts/smoke_test.py https://$GW_FQDN
  docker compose run --rm -v ./scripts:/scripts agent python /scripts/mcp_retrieval_demo.py https://$MCP_FQDN/mcp
Delete all:   az group delete -n $RG --yes --no-wait
EOF
