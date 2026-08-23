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
GITHUB_REPO="${GITHUB_REPO:-knahsirV/garmlink}"
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
  iamcredentials.googleapis.com \
  firestore.googleapis.com

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# --- secrets -----------------------------------------------------------------
# GARMIN_EMAIL and the GitHub OAuth credentials are prompted for, the token blob
# is read from disk, and READYZ_TOKEN is generated. Nothing lands in shell
# history.
echo "==> Creating secrets"
# $3 = "hidden" to suppress echo. Only use it for genuine secrets — a silent
# prompt for a non-secret just looks like the script has hung.
create_secret() {
  local name="$1" prompt="$2" hidden="${3:-}" value
  if gcloud secrets describe "${name}" >/dev/null 2>&1; then
    echo "    ${name} exists — adding a new version"
  else
    gcloud secrets create "${name}" --replication-policy=automatic
  fi
  if [[ "${hidden}" == "hidden" ]]; then
    read -rsp "    ${prompt}: " value
    echo
  else
    read -rp "    ${prompt}: " value
  fi
  if [[ -z "${value}" ]]; then
    echo "    ERROR: ${name} cannot be empty" >&2
    return 1
  fi
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

# The token blob is ~2.8KB of base64 — read it from the file garmlink-auth
# wrote rather than making anyone paste it into a silent prompt.
TOKEN_FILE="${TOKEN_FILE:-${HOME}/.garminconnect/garmin_tokens.json}"
if [[ -f "${TOKEN_FILE}" ]]; then
  echo "    GARMIN_TOKENS_JSON from ${TOKEN_FILE}"
  put_secret GARMIN_TOKENS_JSON "$(base64 < "${TOKEN_FILE}" | tr -d '\n')"
else
  echo "    ${TOKEN_FILE} not found — run 'garmlink-auth' first, or paste the base64 below."
  create_secret GARMIN_TOKENS_JSON "GARMIN_TOKENS_JSON (base64)" hidden
fi

# GitHub OAuth app credentials. Created at
# https://github.com/settings/developers with callback
# https://garmlink-moz6szqd6q-uc.a.run.app/auth/callback
create_secret GITHUB_CLIENT_ID "GITHUB_CLIENT_ID (e.g. Ov23li...)"
create_secret GITHUB_CLIENT_SECRET "GITHUB_CLIENT_SECRET" hidden

# Guards /readyz only. Deliberately NOT the OAuth token: an OAuth access
# token requires a browser flow, and /readyz has to stay reachable from a
# terminal precisely when the OAuth layer is the thing that is broken.
if gcloud secrets describe READYZ_TOKEN >/dev/null 2>&1; then
  echo "    READYZ_TOKEN exists — keeping the current value"
else
  echo "    READYZ_TOKEN generated (64 hex chars)"
  put_secret READYZ_TOKEN "$(openssl rand -hex 32)"
fi

echo "==> Letting the Cloud Run runtime read those secrets"
for s in GARMIN_EMAIL GARMIN_TOKENS_JSON GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET READYZ_TOKEN; do
  gcloud secrets add-iam-policy-binding "${s}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

# --- firestore (OAuth state) -------------------------------------------------
# The default OAuth client store is a file tree on local disk. This service
# scales to zero, so a cold start would wipe every DCR registration and
# refresh-token mapping — in claude.ai that surfaces as "randomly asks me to
# reconnect". Firestore survives cold starts and is correct across instances.
echo "==> Creating the Firestore database"
# Version skew, and it bites harder than it looks. `databases describe` does
# not exist at all on older SDKs (426 has only `create`), so the existence
# check silently fails there and we fall through to create. Worse, on those
# SDKs the *GA* create routes through the App Engine Admin API — it fails
# unless appengine.googleapis.com is enabled, and enabling it plants a
# permanent App Engine app in the project. The beta track on the same SDKs
# talks to the Firestore API directly, which is what we actually want. Probe
# for GA's `--type` as the signal that GA is on the Firestore API path.
if gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  echo "    (default) exists"
else
  if gcloud firestore databases create --help 2>/dev/null | grep -q -- '--type'; then
    fs_cmd=(gcloud firestore databases create)
  else
    fs_cmd=(gcloud beta firestore databases create)
  fi
  # tee rather than capture: this command can prompt for confirmation and can
  # take a couple of minutes, and command substitution would hide both — the
  # prompt included, leaving the script apparently hung on no output at all.
  fs_log="$(mktemp)"
  if "${fs_cmd[@]}" --location="${REGION}" --type=firestore-native 2>&1 | tee "${fs_log}"; then
    echo "    (default) created in ${REGION}"
  elif grep -qiE 'already exists|ALREADY_EXISTS' "${fs_log}"; then
    # Without `describe`, an already-exists error is the only way to find out.
    # Swallowing it is what keeps re-runs of this script idempotent.
    echo "    (default) exists"
  else
    rm -f "${fs_log}"
    exit 1
  fi
  rm -f "${fs_log}"
fi

echo "==> Letting the Cloud Run runtime read and write Firestore"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/datastore.user >/dev/null

# --- deploy service account --------------------------------------------------
echo "==> Creating deploy service account"
if gcloud iam service-accounts describe "${DEPLOY_SA}" >/dev/null 2>&1; then
  echo "    ${DEPLOY_SA_NAME} exists"
else
  # No 2>/dev/null here: a real failure must be visible, not swallowed.
  gcloud iam service-accounts create "${DEPLOY_SA_NAME}" \
    --display-name="GitHub Actions deployer"
fi

# Service account creation is eventually consistent. Binding a role to one that
# has not propagated fails with "does not exist", so wait for it to resolve.
echo "    waiting for the service account to propagate"
for i in $(seq 1 30); do
  if gcloud iam service-accounts describe "${DEPLOY_SA}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# The describe above can succeed before the IAM policy backend agrees, so each
# binding also retries.
bind_role() {
  local role="$1"
  for i in $(seq 1 10); do
    if gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${DEPLOY_SA}" --role="${role}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  echo "    ERROR: could not bind ${role} to ${DEPLOY_SA}" >&2
  return 1
}

for role in \
  roles/run.admin \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.admin \
  roles/storage.admin \
  roles/logging.viewer \
  roles/iam.serviceAccountUser
do
  echo "    binding ${role}"
  bind_role "${role}"
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

Push to main (or run the workflow manually) to deploy.

Access is GitHub OAuth: add <url>/mcp as a custom connector in claude.ai, or
`claude mcp add --transport http garmlink <url>/mcp`, and complete the browser
flow. Only logins in GITHUB_ALLOWED_USERS are admitted. There is no bearer
token to copy.

Check the service without completing an OAuth flow:
  gcloud secrets versions access latest --secret=READYZ_TOKEN --project=${PROJECT_ID}
and send it as the bearer token to <url>/readyz.
DONE
