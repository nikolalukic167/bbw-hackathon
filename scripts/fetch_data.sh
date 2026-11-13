#!/usr/bin/env bash
# Fetch and unpack the PLATO-40 v2 datasets into ./data (gitignored).
#
# The archives are distributed by the organisers (proximata Discord / GitHub
# organisation); they are not redistributed by this repository. Provide them in
# one of two ways:
#
#   scripts/fetch_data.sh /path/to/Hackathon.zip      # the organiser bundle
#   PLATO40_DATA_URL=<url-of-bundle> scripts/fetch_data.sh
#
# Every archive is checked against the SHA-256 sums published with the bundle
# before it is unpacked. The encrypted holdout archive is checked but not opened.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${ROOT}/data"
mkdir -p "${DATA}"

declare -A SHA256=(
  [PLATO40_v2_quickstart.zip]=e7f511adddcaa9ac38459e9e5bef327c09fc7c294645f24b32a46319b73050c0
  [PLATO40_v2_dev.zip]=b423b1dcc2fc157c6a5f4713feb779f310674408e89b012a455d5884c458c0c9
  [PLATO40_v2_holdout_encrypted.7z]=50a8c87d0a41a6d18d081cbce6e0308ff4f8dc923f0bc11d5d513ca7d7b14f88
)

bundle="${1:-}"
if [[ -z "${bundle}" ]]; then
  if [[ -z "${PLATO40_DATA_URL:-}" ]]; then
    echo "usage: $0 <bundle.zip>   or   PLATO40_DATA_URL=<url> $0" >&2
    exit 2
  fi
  bundle="${DATA}/bundle.zip"
  curl --fail --location --output "${bundle}" "${PLATO40_DATA_URL}"
fi

python3 - "${bundle}" "${DATA}" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY

for name in "${!SHA256[@]}"; do
  file="${DATA}/${name}"
  [[ -f "${file}" ]] || { echo "missing ${name} in bundle" >&2; exit 1; }
  actual="$(sha256sum "${file}" | cut -d' ' -f1)"
  if [[ "${actual}" != "${SHA256[${name}]}" ]]; then
    echo "checksum mismatch for ${name}" >&2
    exit 1
  fi
  echo "ok ${name}"
done

for name in PLATO40_v2_quickstart.zip PLATO40_v2_dev.zip; do
  python3 - "${DATA}/${name}" "${DATA}" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
done
echo "datasets in ${DATA}: $(ls -d "${DATA}"/PLATO40_v2_*/ | xargs -n1 basename | tr '\n' ' ')"
