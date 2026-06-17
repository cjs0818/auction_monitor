#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 실행 전 현재 Conda 위치는 보존하고, Python 경로 오염 변수는 제거한다.
SAVED_CONDA_EXE="${CONDA_EXE:-}"
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE
unset PIP_USER PIP_TARGET PIP_PREFIX
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL
unset _CE_CONDA _CE_M
export PYTHONNOUSERSITE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

REQUESTED_PYTHON="${PYTHON_BIN:-}"
VENV_DIR="${VENV_DIR:-$ROOT/.venv}"
PIP_RETRIES="${LANDWATCH_PIP_RETRIES:-3}"
PIP_TIMEOUT="${LANDWATCH_PIP_TIMEOUT:-30}"
OFFLINE_DIR=""
SKIP_INSTALL=0
RECREATE_VENV=0

log() { printf '[LandWatch] %s\n' "$*"; }
warn() { printf '[LandWatch 경고] %s\n' "$*" >&2; }
fail() { printf '[LandWatch 오류] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
LandWatch macOS 설치

사용법:
  ./scripts/setup_mac.sh
  ./scripts/setup_mac.sh --recreate-venv
  ./scripts/setup_mac.sh --offline /path/to/wheelhouse

옵션:
  --recreate-venv       기존 .venv를 삭제하고 깨끗하게 다시 생성
  --offline DIR         인터넷 없이 DIR의 wheel 파일만 사용해 설치
  --skip-install        패키지 설치를 생략
  -h, --help            도움말

환경변수:
  PYTHON_BIN             사용할 비-Conda Python 경로
  VENV_DIR               환경 경로(기본: 프로젝트/.venv)
  LANDWATCH_PIP_RETRIES  pip 재시도 횟수(기본: 3)
  LANDWATCH_PIP_TIMEOUT  pip 제한시간(기본: 30초)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --recreate-venv) RECREATE_VENV=1; shift ;;
    --offline) [[ $# -ge 2 ]] || fail "--offline 뒤에 경로가 필요합니다."; OFFLINE_DIR="$2"; shift 2 ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "알 수 없는 옵션: $1" ;;
  esac
done

python_version_ok() {
  "$1" -I - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

is_conda_python() {
  "$1" -I - <<'PY' >/dev/null 2>&1
import sys
text = " ".join([sys.executable, sys.prefix, sys.base_prefix]).lower()
raise SystemExit(0 if any(x in text for x in ("conda", "miniconda", "anaconda")) else 1)
PY
}

find_clean_python() {
  local candidates=()
  [[ -n "$REQUESTED_PYTHON" ]] && candidates+=("$REQUESTED_PYTHON")
  candidates+=(
    "/opt/homebrew/bin/python3.13"
    "/opt/homebrew/bin/python3.12"
    "/opt/homebrew/bin/python3.11"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3.13"
    "/usr/local/bin/python3.12"
    "/usr/local/bin/python3.11"
    "/usr/local/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
  )
  local cmd candidate
  cmd="$(command -v python3 2>/dev/null || true)"
  [[ -n "$cmd" ]] && candidates+=("$cmd")
  for candidate in "${candidates[@]}"; do
    [[ -x "$candidate" ]] || continue
    python_version_ok "$candidate" || continue
    is_conda_python "$candidate" && continue
    printf '%s' "$candidate"
    return 0
  done
  return 1
}

find_conda() {
  local candidates=()
  [[ -n "$SAVED_CONDA_EXE" ]] && candidates+=("$SAVED_CONDA_EXE")
  candidates+=(
    "$HOME/miniconda3/bin/conda"
    "$HOME/anaconda3/bin/conda"
    "/opt/homebrew/Caskroom/miniconda/base/bin/conda"
    "/opt/homebrew/bin/conda"
    "/usr/local/bin/conda"
  )
  local cmd candidate
  cmd="$(command -v conda 2>/dev/null || true)"
  [[ -n "$cmd" ]] && candidates+=("$cmd")
  for candidate in "${candidates[@]}"; do
    [[ -x "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

CLEAN_PYTHON="$(find_clean_python || true)"
CONDA_CMD="$(find_conda || true)"
BACKEND=""
if [[ -n "$CLEAN_PYTHON" ]]; then
  BACKEND="venv"
  log "독립 Python 확인: $CLEAN_PYTHON"
elif [[ -n "$CONDA_CMD" ]]; then
  BACKEND="conda-prefix"
  log "독립 Python이 없어 전용 Conda 환경을 생성합니다: $CONDA_CMD"
else
  cat >&2 <<'HELP'
[LandWatch 오류] Python 3.10 이상의 독립 Python 또는 conda를 찾지 못했습니다.

Homebrew가 설치되어 있다면:
  brew install python@3.12
  ./scripts/setup_mac.sh --recreate-venv
HELP
  exit 1
fi

is_environment_clean() {
  [[ -x "$VENV_DIR/bin/python" ]] || return 1
  VENV_EXPECTED="$VENV_DIR" "$VENV_DIR/bin/python" -I - <<'PY' >/dev/null 2>&1
import pathlib, os, sys
expected = pathlib.Path(os.environ["VENV_EXPECTED"]).resolve()
if pathlib.Path(sys.prefix).resolve() != expected:
    raise SystemExit(1)
for entry in sys.path:
    if not entry or "site-packages" not in entry:
        continue
    p = pathlib.Path(entry).resolve()
    try:
        p.relative_to(expected)
    except ValueError:
        raise SystemExit(1)
# 핵심 모듈이 설치돼 있다면 모두 현재 환경 안에 있어야 한다.
for name in ("streamlit", "starlette"):
    try:
        module = __import__(name)
    except Exception:
        continue
    p = pathlib.Path(module.__file__).resolve()
    try:
        p.relative_to(expected)
    except ValueError:
        raise SystemExit(1)
raise SystemExit(0)
PY
}

if [[ -d "$VENV_DIR" ]] && ! is_environment_clean; then
  warn "기존 .venv가 ros2/Conda 또는 외부 site-packages를 참조하여 재생성합니다."
  RECREATE_VENV=1
fi

if [[ "$RECREATE_VENV" -eq 1 && -d "$VENV_DIR" ]]; then
  log "기존 환경 삭제: $VENV_DIR"
  rm -rf "$VENV_DIR"
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  if [[ "$BACKEND" == "venv" ]]; then
    log "표준 가상환경 생성: $VENV_DIR"
    "$CLEAN_PYTHON" -I -m venv --clear "$VENV_DIR"
  else
    log "프로젝트 전용 Conda 환경 생성: $VENV_DIR"
    "$CONDA_CMD" create -y --no-default-packages --prefix "$VENV_DIR" python=3.11 pip
    touch "$VENV_DIR/.landwatch-conda-prefix"
  fi
else
  log "정상 환경 재사용: $VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
[[ -x "$VENV_PYTHON" ]] || fail "환경 Python 생성에 실패했습니다: $VENV_PYTHON"
is_environment_clean || fail "환경 격리에 실패했습니다. $VENV_PYTHON 이 외부 패키지를 참조합니다."

if ! "$VENV_PYTHON" -I -m pip --version >/dev/null 2>&1; then
  log "pip 복구"
  "$VENV_PYTHON" -I -m ensurepip --upgrade
fi

pip_install() {
  "$VENV_PYTHON" -I -m pip --isolated "$@"
}

install_online() {
  local index_url="${PIP_INDEX_URL:-https://pypi.org/simple}"
  local attempt=1
  while (( attempt <= PIP_RETRIES )); do
    log "Python 패키지 설치 (${attempt}/${PIP_RETRIES})"
    if pip_install install --upgrade --upgrade-strategy eager --prefer-binary \
      --retries 2 --timeout "$PIP_TIMEOUT" --index-url "$index_url" -r requirements.txt; then
      return 0
    fi
    (( attempt < PIP_RETRIES )) && sleep $((attempt * 5))
    attempt=$((attempt + 1))
  done
  return 1
}

install_offline() {
  [[ -d "$OFFLINE_DIR" ]] || fail "wheelhouse를 찾을 수 없습니다: $OFFLINE_DIR"
  pip_install install --upgrade --no-index --find-links "$OFFLINE_DIR" -r requirements.txt
}

verify_environment() {
  VENV_EXPECTED="$VENV_DIR" "$VENV_PYTHON" -I - <<'PY'
import importlib, importlib.metadata, pathlib, os, sys
expected = pathlib.Path(os.environ["VENV_EXPECTED"]).resolve()
modules = ("streamlit", "starlette", "pandas", "yaml", "requests", "dateutil", "selenium", "truststore")
errors = []
for name in modules:
    try:
        mod = importlib.import_module(name)
        path = pathlib.Path(mod.__file__).resolve()
        path.relative_to(expected)
    except Exception as exc:
        errors.append(f"{name}: {exc}")
try:
    from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES
except Exception as exc:
    errors.append(f"starlette gzip 호환성: {exc}")
if errors:
    raise SystemExit("환경 검증 실패: " + " | ".join(errors))
print("[LandWatch] 환경 검증 완료")
print("  Python:", sys.executable)
print("  Streamlit:", importlib.metadata.version("streamlit"))
print("  Starlette:", importlib.metadata.version("starlette"))
print("  Truststore:", importlib.metadata.version("truststore"))
PY
}

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  if [[ -n "$OFFLINE_DIR" ]]; then install_offline; else install_online || fail "패키지 설치에 실패했습니다."; fi
fi
verify_environment

[[ -f config/config.yaml ]] || cp config/config.example.yaml config/config.yaml
mkdir -p reports logs data/selenium_debug

cat <<EOF_DONE

설치 완료
- 환경: $VENV_DIR
- 실행 Python: $VENV_PYTHON

대시보드 실행:
  $VENV_PYTHON -I -m streamlit run app.py

연결 확인:
  $VENV_PYTHON -I -m landwatch.cli selenium-check
EOF_DONE
