#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL
unset _CE_CONDA _CE_M
export PYTHONNOUSERSITE=1
if [[ ! -x .venv/bin/python ]]; then
  echo "환경이 없습니다. ./scripts/setup_mac.sh --recreate-venv 를 먼저 실행하십시오."
  read -r -p "Enter 키를 누르면 종료합니다."
  exit 1
fi
exec .venv/bin/python -I -m streamlit run app.py
