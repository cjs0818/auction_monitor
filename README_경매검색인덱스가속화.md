# 경매 검색 인덱스 가속화 (CAUCA 방식 참고)

## 1) 최소 변경 설계안
- 기존 실시간 법원 수집 경로는 유지한다.
- 단, 같은 검색조건 반복 실행 시에는 최근 수집 목록을 DB 인덱스에서 재사용한다.
- 인덱스 키는 검색 결과에 영향을 주는 조건(지역/상태/용도/가격/면적/유찰/할인율/기간/키워드)만 반영한다.
- 상세조회/사진 보강/최종 필터는 기존 로직을 그대로 적용한다.

## 2) 1차 구현 범위
- DB 테이블: `court_search_index`
- 런타임 경로:
  - `search_index_enabled=true` + `경매` + `force_refresh=false`면 인덱스 우선
  - 인덱스 미스 또는 만료 시 기존 `provider.fetch()` 실행 후 인덱스 저장
- UI 설정 추가:
  - 경매 검색 인덱스 재사용
  - 인덱스 유효시간(분)

## 3) 성능측정 시나리오
스크립트: `scripts/perf_search_index.py`

### 실행 예시
```bash
/Users/jschoi/work/auction_monitor/.venv/bin/python scripts/perf_search_index.py \
  --config config/config.yaml \
  --target 경매 \
  --profile "지방 소액 농지·임야"
```

### 출력 지표
- `cold_run_seconds`: 강제 새로조회 시간
- `warm_run_seconds`: 인덱스 재사용 시간
- `speedup_x`: 가속 배수
- `cold_diagnostics` / `warm_diagnostics`: 프로필별 진단 로그

## 운영 팁
- 인덱스는 속도 최적화용이다. 데이터 최신성이 중요한 경우 `force_refresh`를 사용한다.
- 인덱스 TTL은 10~60분 사이에서 운영 환경에 맞게 조정한다.
