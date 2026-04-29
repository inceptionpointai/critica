#!/usr/bin/env bash
# One-time bootstrap of the critica writer role + tables in analytics-db.
#
# Generates a fresh password locally, applies the migration via kubectl
# exec into the analytics-db primary, and stores the connection URL in
# SSM. Idempotent — re-running rotates the password.
#
# Requires:
#   - aws CLI with ssm:PutParameter on /prod/critica/*
#   - kubectl with exec into prod/analytics-db-* (postgres container)
set -euo pipefail

REGION=${AWS_REGION:-us-west-2}
NS=${NAMESPACE:-prod}
SSM_NAME=${SSM_NAME:-/prod/critica/analytics-db-url}
DB_HOST=${DB_HOST:-analytics-db-rw.prod.svc.cluster.local}
DB_NAME=${DB_NAME:-analytics}

here=$(cd "$(dirname "$0")" && pwd)
sql="$here/0001_init.sql"
[[ -f "$sql" ]] || { echo "missing $sql" >&2; exit 1; }

echo ">> finding analytics-db primary pod in $NS"
pod=$(kubectl -n "$NS" get pod \
  -l cnpg.io/cluster=analytics-db,cnpg.io/instanceRole=primary \
  -o jsonpath='{.items[0].metadata.name}')
[[ -n "$pod" ]] || { echo "no primary pod found" >&2; exit 1; }
echo "   pod=$pod"

echo ">> generating password"
pass=$(openssl rand -base64 30 | tr -d '+/=' | head -c 40)

echo ">> applying $sql"
kubectl -n "$NS" exec -i "$pod" -c postgres -- \
  psql -U postgres -d "$DB_NAME" -v ON_ERROR_STOP=1 -v "pass=$pass" -f - < "$sql"

url="postgresql://critica_writer:${pass}@${DB_HOST}:5432/${DB_NAME}?sslmode=require"

echo ">> putting connection URL in SSM at $SSM_NAME"
aws ssm put-parameter \
  --region "$REGION" \
  --name "$SSM_NAME" \
  --type SecureString \
  --value "$url" \
  --overwrite >/dev/null

echo ">> verifying"
kubectl -n "$NS" exec "$pod" -c postgres -- \
  psql -U postgres -d "$DB_NAME" -c \
  "SELECT count(*) AS rows FROM critica.episode_reviews;"

cat <<NEXT
Bootstrap complete.

Next:
  1. ExternalSecret will sync $SSM_NAME into critica-secrets on its next
     refresh (1h, or annotate force-sync to trigger now).
  2. Pods need a rollout-restart after the secret syncs to pick up the
     new ANALYTICS_DB_URL env var.
  3. critica_writer role has INSERT+SELECT on the two tables (SELECT is
     required for ON CONFLICT DO NOTHING — see qualitas postmortem).
NEXT
