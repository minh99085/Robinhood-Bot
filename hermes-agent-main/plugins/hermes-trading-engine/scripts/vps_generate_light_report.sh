#!/usr/bin/env bash
# Permanent VPS light-report runner for the Hermes Trading Engine (PAPER ONLY).
#
# One command, self-bootstrapping. It NEVER relies on system python or ad-hoc manual
# `pip install`: it creates/updates a dedicated `.report_venv`, installs every report +
# test dependency from the repo requirements files (plus pytest/pydantic/numpy
# explicitly), then uses `.report_venv/bin/python` for report generation + validation.
#
#   bash scripts/vps_generate_light_report.sh
#
# It refreshes runtime_data from the running container, regenerates the light report,
# runs validation, and packages a UNIQUE timestamped zip while also updating
# `vps_light_report_latest.zip`. It does NOT change trading flags, gates, paper-realism,
# or live-trading behavior, and it never places an order.
set -euo pipefail

# --- locate the plugin folder (this script lives in <plugin>/scripts) ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PLUGIN_DIR}"

CONTAINER="${HERMES_TRAINING_CONTAINER:-hermes-training}"
VENV="${PLUGIN_DIR}/.report_venv"
VPY="${VENV}/bin/python"
TS="$(date -u +%Y%m%d_%H%M%S)"
LOG_DIR="${PLUGIN_DIR}/report_logs"
REPORT_LOG="${LOG_DIR}/report_${TS}.log"
VALIDATION_OUT="${PLUGIN_DIR}/validation_light_latest.txt"
ZIP_NAME="vps_light_report_${TS}.zip"
ZIP_LATEST="vps_light_report_latest.zip"

mkdir -p "${LOG_DIR}"

echo "==> Hermes VPS light-report runner"
echo "    plugin   : ${PLUGIN_DIR}"
echo "    container: ${CONTAINER}"
echo "    venv     : ${VENV}"

# --- 1) self-bootstrapping report venv (never touches system python) -----------
PYBOOT="$(command -v python3 || command -v python || true)"
if [[ -z "${PYBOOT}" ]]; then
  echo "FATAL: no python3 on PATH to bootstrap the report venv." >&2
  exit 3
fi
if [[ ! -x "${VPY}" ]]; then
  echo "==> creating report venv (.report_venv)"
  "${PYBOOT}" -m venv "${VENV}"
fi
echo "==> installing report/test dependencies into .report_venv (no system pip)"
"${VPY}" -m pip install --upgrade pip >/dev/null
# install from the repo requirements first, then pin the must-have report/test deps
[[ -f requirements.txt ]]     && "${VPY}" -m pip install -r requirements.txt
[[ -f requirements-dev.txt ]] && "${VPY}" -m pip install -r requirements-dev.txt
# explicit belt-and-suspenders: the exact modules whose absence broke run-ready before
"${VPY}" -m pip install "pytest>=8,<10" "pydantic>=2.6,<3" "numpy>=1.26,<3" \
                        "fastapi>=0.110,<1" "httpx>=0.27,<1"

# --- 2) fail FAST with a clear message if the env is still incomplete -----------
echo "==> verifying report dependencies are importable in .report_venv"
if ! "${VPY}" - <<'PYCHECK'
import sys
sys.path.insert(0, "scripts")
from inspection_collectors import report_dependency_status
st = report_dependency_status()
if not st["ok"]:
    print("DEPENDENCY SETUP INCOMPLETE: " + st["message"], file=sys.stderr)
    sys.exit(11)
print("report dependencies OK: " + ", ".join(st["required"]))
PYCHECK
then
  echo "FATAL: report venv is missing required dependencies (see above). Aborting." >&2
  exit 11
fi

# --- 3) stale-status protection: show container health BEFORE the report -------
echo "==> docker compose ps"
docker compose ps || docker ps || echo "   (docker compose ps unavailable)"
echo "==> ${CONTAINER} recent logs (tail)"
docker logs --tail 40 "${CONTAINER}" 2>&1 || echo "   (could not read ${CONTAINER} logs — is it running?)"
CONTAINER_STATE="$(docker inspect -f '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || echo absent)"
CONTAINER_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${CONTAINER}" 2>/dev/null || echo unknown)"
echo "==> ${CONTAINER}: state=${CONTAINER_STATE} health=${CONTAINER_HEALTH}"
if [[ "${CONTAINER_STATE}" != "running" ]]; then
  echo "WARNING: ${CONTAINER} is '${CONTAINER_STATE}' (not running). runtime_data may be" \
       "stale/incomplete and the report may classify status as stale." | tee -a "${REPORT_LOG}"
fi

# --- 4) refresh runtime_data from the container --------------------------------
echo "==> refreshing runtime_data from ${CONTAINER}:/data"
rm -rf runtime_data
if ! docker cp "${CONTAINER}:/data" runtime_data; then
  echo "FATAL: docker cp ${CONTAINER}:/data failed — container stopped or /data missing." >&2
  exit 12
fi
# take ownership so the venv user can read/regenerate artifacts
chown -R "$(id -u):$(id -g)" runtime_data 2>/dev/null || true

# --- 5) regenerate the light report + validation (venv python ONLY) ------------
echo "==> deleting old inspection_reports"
rm -rf inspection_reports

echo "==> generating light-mode inspection report (.report_venv python)"
set +e
"${VPY}" scripts/generate_bot_inspection_report.py \
    --output inspection_reports --data-dir runtime_data --bundle-mode light \
    2>&1 | tee "${REPORT_LOG}"
REPORT_RC=${PIPESTATUS[0]}
echo "==> running training-runtime validation (.report_venv python)"
"${VPY}" scripts/validate_training_runtime.py --data-dir runtime_data \
    2>&1 | tee "${VALIDATION_OUT}"
VALIDATE_RC=${PIPESTATUS[0]}
set -e
echo "report_rc=${REPORT_RC} validate_rc=${VALIDATE_RC}" | tee -a "${REPORT_LOG}"

# --- 6) package a UNIQUE zip + update the 'latest' pointer ----------------------
echo "==> packaging ${ZIP_NAME}"
ZIP_INPUTS=()
[[ -d inspection_reports ]]   && ZIP_INPUTS+=("inspection_reports")
[[ -d runtime_data/metrics ]] && ZIP_INPUTS+=("runtime_data/metrics")
[[ -f runtime_data/inspection_summary.json ]] && ZIP_INPUTS+=("runtime_data/inspection_summary.json")
[[ -f "${VALIDATION_OUT}" ]]  && ZIP_INPUTS+=("validation_light_latest.txt")
[[ -d report_logs ]]          && ZIP_INPUTS+=("report_logs")
rm -f "${ZIP_NAME}"
if command -v zip >/dev/null 2>&1; then
  zip -r "${ZIP_NAME}" "${ZIP_INPUTS[@]}"
else
  # fallback: python's zipfile (no system 'zip' needed)
  "${VPY}" - "$ZIP_NAME" "${ZIP_INPUTS[@]}" <<'PYZIP'
import sys, os, zipfile
dest, inputs = sys.argv[1], sys.argv[2:]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
    for p in inputs:
        if os.path.isfile(p):
            zf.write(p)
        else:
            for root, _d, files in os.walk(p):
                for f in files:
                    zf.write(os.path.join(root, f))
print("wrote", dest)
PYZIP
fi
cp -f "${ZIP_NAME}" "${ZIP_LATEST}"

echo "==> DONE"
echo "    report_rc=${REPORT_RC} validate_rc=${VALIDATE_RC}"
echo "    unique zip : ${PLUGIN_DIR}/${ZIP_NAME}"
echo "    latest zip : ${PLUGIN_DIR}/${ZIP_LATEST}"
echo "    upload ${ZIP_LATEST} to ChatGPT for inspection."
# Surface the report generator's own exit code so run-ready gating is NOT hidden.
exit "${REPORT_RC}"
