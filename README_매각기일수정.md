# 사건 직접조회 매각기일 표시 수정본

법원 사건상세 응답의 현재 예정기일과 과거 기일내역을 모두 읽도록 수정했습니다.

- 현재 예정기일: `dlt_dspslGdsDspslObjctLst[].dspslDxdyYmd`
- 과거 기일내역: `dlt_rletCsGdsDtsDxdyInf[].dxdyYmd`

대시보드의 `특정 사건번호 직접 확인`에서 사건번호를 조회하면 다음매각기일이 사건 요약에 표시되고, 매각기일내역 표에는 현재 예정기일이 먼저 표시됩니다.
