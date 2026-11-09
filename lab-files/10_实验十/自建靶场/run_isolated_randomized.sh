#!/usr/bin/env bash
# Generate per-run fictional candidates, then call the reviewed namespace-only Lab 10 harness.
set -Eeuo pipefail
umask 077
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ROOT=$(cd -- "$HERE/.." && pwd -P)
PYTHON=python3
VENDOR=
HYDRA=
EVIDENCE=
while (($#)); do
  case "$1" in
    --python) PYTHON=${2:?}; shift 2;;
    --vendor) VENDOR=${2:?}; shift 2;;
    --hydra) HYDRA=${2:?}; shift 2;;
    --evidence) EVIDENCE=${2:?}; shift 2;;
    -h|--help)
      printf '%s\n' 'Usage: sudo bash run_isolated_randomized.sh [--python PATH] [--vendor DIR] [--hydra PATH] [--evidence NEW_TAR]'
      exit 0;;
    *) printf 'Unsupported option: %s\n' "$1" >&2; exit 2;;
  esac
done
[[ $EUID -eq 0 ]] || { echo 'Run only inside the dedicated lab M VM.' >&2; exit 2; }
for tool in od tr; do command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 2; }; done
RUN=$(mktemp -d /tmp/lab10-fictional-candidates.XXXXXX)
chmod 700 "$RUN"
cleanup() { rm -f -- "$RUN/candidates.txt"; rmdir -- "$RUN" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
SEED=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
[[ ${#SEED} -eq 32 ]] || { echo 'Could not create a fresh fictional candidate seed.' >&2; exit 2; }
for index in {1..18}; do printf 'L10-%s-%02d\n' "$SEED" "$index"; done > "$RUN/candidates.txt"
chmod 600 "$RUN/candidates.txt"
ARGS=(--dictionary "$RUN/candidates.txt" --client "$ROOT/lab10_bruteforce.py" --target-index 4 --max-attempts 18 --python "$PYTHON")
[[ -z $VENDOR ]] || ARGS+=(--vendor "$VENDOR")
[[ -z $HYDRA ]] || ARGS+=(--hydra "$HYDRA")
[[ -z $EVIDENCE ]] || ARGS+=(--evidence "$EVIDENCE")
bash "$HERE/isolated_openssh_trial.sh" "${ARGS[@]}"