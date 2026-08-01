#!/usr/bin/env bash
set -eu

for argument in "$@"; do
  if [ "$argument" = "--builder" ]; then
    echo "build_rootless_image.sh always selects --builder podman-rootless" >&2
    exit 2
  fi
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$script_dir/build_isolated_image.sh" --builder podman-rootless "$@"
