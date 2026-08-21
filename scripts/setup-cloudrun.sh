#!/usr/bin/env bash
# One-time Google Cloud setup for deploying garmlink to Cloud Run.
#
# Run this once, from your own gcloud login (not a service account):
#   gcloud auth login
#   ./scripts/setup-cloudrun.sh
#
# It is idempotent — re-running is safe.

set -euo pipefail

# ---- edit these -------------------------------------------------------------
PROJECT_ID="${PROJECT_ID:-garmlink}"        # must be globally unique
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-garmlink}"
GITHUB_OWNER="${GITHUB_OWNER:-knahsirV}"
GITHUB_REPO="${GITHUB_REPO:-knahsirV/garmin-mcp}"
POOL="${POOL:-github}"
PROVIDER="${PROVIDER:-github-oidc}"
DEPLOY_SA_NAME="${DEPLOY_SA_NAME:-gh-deployer}"
# -----------------------------------------------------------------------------

DEPLOY_SA="${DEPLOY_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> Using project ${PROJECT_ID} in ${REGION}"
gcloud config set project "${PROJECT_ID}"

echo "==> Enabling APIs (takes a minute the first time)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# --- secrets -----------------------------------------------------------------
# GARMIN_EMAIL is prompted for; the token blob is read from disk and the bearer
# token is generated. Nothing lands in shell history.
echo "==> Creating secrets"
create_secret() {
  local name="$1" prompt="$2" value
  if gcloud secrets describe "${name}" >/dev/null 2>&1; then
    echo "    ${name} exists — adding a new version"
  else
    gcloud secrets create "${name}" --replication-policy=automatic
  fi
  read -rsp "    ${prompt}: " value
  echo
  printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=-
}
# Stores a value produced by a command, rather than typed at a prompt.
put_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "${name}" >/dev/null 2>&1; then
    echo "    ${name} exists — adding a new version"
  else
    gcloud secrets create "${name}" --replication-policy=automatic
  fi
  printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=-
}

create_secret GARMIN_EMAIL "GARMIN_EMAIL"

# The token blob is ~2.8KB of base64 — read it from the file garmin-mcp-auth
# wrote rather than making anyone paste it into a silent prompt.
TOKEN_FILE="${TOKEN_FILE:-${HOME}/.garminconnect/garmin_tokens.json}"
if [[ -f "${TOKEN_FILE}" ]]; then
  echo "    GARMIN_TOKENS_JSON from ${TOKEN_FILE}"
  put_secret GARMIN_TOKENS_JSON "$(base64 < "${TOKEN_FILE}" | tr -d '\n')"
else
  echo "    ${TOKEN_FILE} not found — run 'garmin-mcp-auth' first, or paste the base64 below."
  create_secret GARMIN_TOKENS_JSON "GARMIN_TOKENS_JSON (base64)"
fi

# Generated rather than typed. Retrieve it later with:
#   gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN
if gcloud secrets describe MCP_AUTH_TOKEN >/dev/null 2>&1; then
  echo "    MCP_AUTH_TOKEN exists — keeping the current value"
else
  echo "    MCP_AUTH_TOKEN generated (64 hex chars)"
  put_secret MCP_AUTH_TOKEN "$(openssl rand -hex 32)"
fi

echo "==> Letting the Cloud Run runtime read those secrets"
for s in GARMIN_EMAIL GARMIN_TOKENS_JSON MCP_AUTH_TOKEN; do
  gcloud secrets add-iam-policy-binding "${s}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

# --- deploy service account --------------------------------------------------
echo "==> Creating deploy service account"
gcloud iam service-accounts create "${DEPLOY_SA_NAME}" \
  --display-name="GitHub Actions deployer" 2>/dev/null || echo "    exists"

for role in \
  roles/run.admin \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.admin \
  roles/storage.admin \
  roles/logging.viewer \
  roles/iam.serviceAccountUser
do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${DEPLOY_SA}" --role="${role}" >/dev/null
done

# --- workload identity federation (keyless GitHub -> GCP auth) ---------------
echo "==> Configuring Workload Identity Federation"
gcloud iam workload-identity-pools create "${POOL}" \
  --location=global --display-name="GitHub Actions" 2>/dev/null || echo "    pool exists"

gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
  --location=global \
  --workload-identity-pool="${POOL}" \
  --display-name="GitHub OIDC" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner == '${GITHUB_OWNER}'" \
  2>/dev/null || echo "    provider exists"

# Only this one repo may impersonate the deployer.
gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA}" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" >/dev/null

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

# --- artifact registry cleanup (free tier is 0.5 GB) -------------------------
echo "==> Adding an image cleanup policy (keep newest 2)"
POLICY_FILE="$(mktemp)"
cat > "${POLICY_FILE}" <<'JSON'
[
  {
    "name": "keep-recent",
    "action": {"type": "Keep"},
    "mostRecentVersions": {"keepCount": 2}
  },
  {
    "name": "delete-rest",
    "action": {"type": "Delete"},
    "condition": {"olderThan": "7d"}
  }
]
JSON
gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy \
  --location="${REGION}" --policy="${POLICY_FILE}" 2>/dev/null \
  || echo "    repo not created yet — re-run this after the first deploy"
rm -f "${POLICY_FILE}"

# --- hand the values to GitHub ----------------------------------------------
echo "==> Setting GitHub repo variables"
gh variable set GCP_PROJECT_ID --repo "${GITHUB_REPO}" --body "${PROJECT_ID}"
gh variable set GCP_WIF_PROVIDER --repo "${GITHUB_REPO}" --body "${WIF_PROVIDER}"
gh variable set GCP_DEPLOY_SA --repo "${GITHUB_REPO}" --body "${DEPLOY_SA}"

cat <<DONE

Setup complete.

  project   ${PROJECT_ID}
  region    ${REGION}
  service   ${SERVICE}
  deployer  ${DEPLOY_SA}

Read your bearer token with:
  gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN --project=${PROJECT_ID}

Push to main (or run the workflow manually) to deploy. The service URL is
printed at the end of the workflow run; put it in your Claude Desktop config
as <url>/mcp with that token as the Authorization bearer value.
DONE
