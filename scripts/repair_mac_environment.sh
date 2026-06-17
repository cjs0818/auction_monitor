#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "[LandWatch] ros2/Conda 혼합 환경을 제거하고 프로젝트 환경을 다시 만듭니다."
exec "$ROOT/scripts/setup_mac.sh" --recreate-venv
