#!/usr/bin/env bash
set -eu

usage() {
  echo "usage: $0 --base-image IMAGE@sha256:DIGEST --tag TAG --rtlbench-commit SHA --python-version VERSION --iverilog-package-version VERSION --iverilog-version VERSION --vvp-version VERSION --verilator-package-version VERSION --verilator-version VERSION --yosys-package-version VERSION --yosys-version VERSION" >&2
}

base_image=
tag=
rtlbench_commit=
python_version=
iverilog_version=
iverilog_package_version=
vvp_version=
verilator_version=
verilator_package_version=
yosys_version=
yosys_package_version=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-image) base_image=${2:?missing value for --base-image}; shift 2 ;;
    --tag) tag=${2:?missing value for --tag}; shift 2 ;;
    --rtlbench-commit) rtlbench_commit=${2:?missing value for --rtlbench-commit}; shift 2 ;;
    --python-version) python_version=${2:?missing value for --python-version}; shift 2 ;;
    --iverilog-package-version) iverilog_package_version=${2:?missing value for --iverilog-package-version}; shift 2 ;;
    --iverilog-version) iverilog_version=${2:?missing value for --iverilog-version}; shift 2 ;;
    --vvp-version) vvp_version=${2:?missing value for --vvp-version}; shift 2 ;;
    --verilator-package-version) verilator_package_version=${2:?missing value for --verilator-package-version}; shift 2 ;;
    --verilator-version) verilator_version=${2:?missing value for --verilator-version}; shift 2 ;;
    --yosys-package-version) yosys_package_version=${2:?missing value for --yosys-package-version}; shift 2 ;;
    --yosys-version) yosys_version=${2:?missing value for --yosys-version}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

if [ -z "$base_image" ] || [ -z "$tag" ] || [ -z "$rtlbench_commit" ] \
  || [ -z "$python_version" ] || [ -z "$iverilog_package_version" ] \
  || [ -z "$iverilog_version" ] || [ -z "$vvp_version" ] \
  || [ -z "$verilator_package_version" ] || [ -z "$verilator_version" ] \
  || [ -z "$yosys_package_version" ] || [ -z "$yosys_version" ]; then
  usage
  exit 2
fi

case "$base_image" in
  *@sha256:*)
    base_digest=${base_image##*@sha256:}
    if [[ ! "$base_digest" =~ ^[0-9a-fA-F]{64}$ ]]; then
      echo "--base-image must include a 64-character SHA-256 digest" >&2
      exit 2
    fi
    ;;
  *) echo "--base-image must include an immutable sha256 digest" >&2; exit 2 ;;
esac
if [[ ! "$rtlbench_commit" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "--rtlbench-commit must be a 40-character git SHA" >&2
  exit 2
fi
if [ "$(git rev-parse HEAD 2>/dev/null || true)" != "$rtlbench_commit" ]; then
  echo "--rtlbench-commit does not match the checked-out source HEAD" >&2
  exit 2
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "the RTLBench source tree must be clean before building the image" >&2
  exit 2
fi
if ! command -v podman >/dev/null 2>&1; then
  echo "rootless Podman is required to build the runner image" >&2
  exit 2
fi
if [ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || true)" != "true" ]; then
  echo "Podman must report rootless=true to build the runner image" >&2
  exit 2
fi

podman build \
  --pull=never \
  --tag "$tag" \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "RTLBench_COMMIT=$rtlbench_commit" \
  --build-arg "RUNNER_CONFIG_VERSION=rtlbench_rootless_runner_v0.1" \
  --build-arg "PYTHON_VERSION=$python_version" \
  --build-arg "IVERILOG_PACKAGE_VERSION=$iverilog_package_version" \
  --build-arg "IVERILOG_VERSION=$iverilog_version" \
  --build-arg "VVP_VERSION=$vvp_version" \
  --build-arg "VERILATOR_PACKAGE_VERSION=$verilator_package_version" \
  --build-arg "VERILATOR_VERSION=$verilator_version" \
  --build-arg "YOSYS_PACKAGE_VERSION=$yosys_package_version" \
  --build-arg "YOSYS_VERSION=$yosys_version" \
  --file runner/Dockerfile \
  .

echo "Run only the digest-qualified image reference printed by podman image inspect."
