# Conda ros2 환경 충돌 수정

오류 예:

```text
ImportError: cannot import name 'DEFAULT_EXCLUDED_CONTENT_TYPES'
```

원인은 프로젝트 `.venv`가 `~/miniconda3/envs/ros2/lib/.../site-packages`를 함께 읽는 것입니다.

## 복구

```bash
cd /Users/jschoi/work/land_auction_monitor
./scripts/repair_mac_environment.sh
.venv/bin/python -I -m streamlit run app.py
```

새 설치 스크립트는 비-Conda Python이 있으면 표준 venv를 만들고, 없으면 기존 ros2 환경을 재사용하지 않고 프로젝트 폴더에 별도의 Conda prefix 환경을 생성합니다.
