#!/usr/bin/env bash
# Build backend + frontend images, push to ECR, and force new ECS deployments.
# Requires: docker, aws CLI, terraform (initialized in infra/ with applied state).
#
# Run from anywhere — paths are resolved from this script's location (repo root = parent of scripts/).
#
# Common mistake (breaks build + still pushes old image if you run commands separately):
#   cd ../frontend   # WRONG from repo root — there is no Desktop/frontend next to the repo
#   cd frontend      # RIGHT: frontend lives at chief-of-staff/frontend
#
# Usage:
#   ./scripts/deploy-ecs-images.sh
#   ./scripts/deploy-ecs-images.sh --skip-ecs          # build + push only
#   AWS_REGION=us-east-1 ./scripts/deploy-ecs-images.sh
#
# Optional: export AWS_REGION if not set in infra/terraform.tfvars as aws_region.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${ROOT}/infra"
BACKEND_DIR="${ROOT}/backend"
FRONTEND_DIR="${ROOT}/frontend"
BACKEND_DOCKERFILE="${BACKEND_DIR}/Dockerfile"
FRONTEND_DOCKERFILE="${FRONTEND_DIR}/Dockerfile"

SKIP_ECS="0"
IMAGE_TAG="latest"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ecs)
      SKIP_ECS="1"
      shift
      ;;
    --tag)
      IMAGE_TAG="${2:?--tag requires a value}"
      shift 2
      ;;
    -h | --help)
      echo "Usage: $0 [--skip-ecs] [--tag TAG]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "${INFRA_DIR}" ]]; then
  echo "Expected infra at ${INFRA_DIR}" >&2
  exit 1
fi

if [[ ! -f "${BACKEND_DOCKERFILE}" ]]; then
  echo "Missing backend Dockerfile at ${BACKEND_DOCKERFILE}" >&2
  exit 1
fi

if [[ ! -f "${FRONTEND_DOCKERFILE}" ]]; then
  echo "Missing frontend Dockerfile at ${FRONTEND_DOCKERFILE}" >&2
  echo "If you meant to build manually, use: cd ${FRONTEND_DIR} && docker build ..." >&2
  exit 1
fi

tf_out() {
  (cd "${INFRA_DIR}" && terraform output -raw "$1")
}

REGION="${AWS_REGION:-}"
if [[ -z "${REGION}" ]] && [[ -f "${INFRA_DIR}/terraform.tfvars" ]]; then
  REGION="$(grep -E '^[[:space:]]*aws_region[[:space:]]*=' "${INFRA_DIR}/terraform.tfvars" | head -1 | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/')"
fi
REGION="${REGION:-us-east-1}"

ECR_BACKEND="$(tf_out ecr_backend_url)"
ECR_FRONTEND="$(tf_out ecr_frontend_url)"
BACKEND_URL="$(tf_out backend_url)"
CLUSTER_NAME="$(tf_out ecs_cluster_name)"

# Cluster is "<prefix>-cluster"; services are "<prefix>-backend" and "<prefix>-frontend"
if [[ "${CLUSTER_NAME}" != *-cluster ]]; then
  echo "Unexpected ecs_cluster_name format: ${CLUSTER_NAME}" >&2
  exit 1
fi
PREFIX="${CLUSTER_NAME%-cluster}"
BACKEND_SERVICE="${PREFIX}-backend"
FRONTEND_SERVICE="${PREFIX}-frontend"

REGISTRY_HOST="${ECR_BACKEND%%/*}"

echo "Repo root:        ${ROOT}"
echo "Backend context:  ${BACKEND_DIR}"
echo "Frontend context: ${FRONTEND_DIR}"
echo "Region:           ${REGION}"
echo "ECR backend:      ${ECR_BACKEND}"
echo "ECR frontend:     ${ECR_FRONTEND}"
echo "NEXT_PUBLIC_API:  ${BACKEND_URL}"
echo "ECS cluster:      ${CLUSTER_NAME}"
echo "ECS services:     ${BACKEND_SERVICE}, ${FRONTEND_SERVICE}"
echo "Image tag:        ${IMAGE_TAG}"
echo ""

echo "Logging in to ECR..."
aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${REGISTRY_HOST}"

echo "Building backend image..."
docker build -f "${BACKEND_DOCKERFILE}" -t "${ECR_BACKEND}:${IMAGE_TAG}" "${BACKEND_DIR}"

echo "Pushing backend image..."
docker push "${ECR_BACKEND}:${IMAGE_TAG}"

echo "Building frontend image..."
docker build \
  -f "${FRONTEND_DOCKERFILE}" \
  --build-arg "NEXT_PUBLIC_API_BASE_URL=${BACKEND_URL}" \
  -t "${ECR_FRONTEND}:${IMAGE_TAG}" \
  "${FRONTEND_DIR}"

echo "Pushing frontend image..."
docker push "${ECR_FRONTEND}:${IMAGE_TAG}"

if [[ "${SKIP_ECS}" == "1" ]]; then
  echo "Skipping ECS rollout (--skip-ecs)."
  exit 0
fi

echo "Forcing new ECS deployment (backend)..."
aws ecs update-service \
  --region "${REGION}" \
  --cluster "${CLUSTER_NAME}" \
  --service "${BACKEND_SERVICE}" \
  --force-new-deployment \
  --no-cli-pager

echo "Forcing new ECS deployment (frontend)..."
aws ecs update-service \
  --region "${REGION}" \
  --cluster "${CLUSTER_NAME}" \
  --service "${FRONTEND_SERVICE}" \
  --force-new-deployment \
  --no-cli-pager

echo "Done. ECS is rolling out new tasks."
