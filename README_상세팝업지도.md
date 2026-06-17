# 검색결과 상세 팝업·지도 업데이트

검색결과 또는 저장된 결과의 행을 클릭하면 모달 창이 열리고, 해당 경매물건의 상세정보와 주소 기반 지도를 표시합니다.

## 적용

실행 중인 Streamlit을 `Ctrl+C`로 종료한 후:

```bash
chmod +x apply_detail_popup.sh
./apply_detail_popup.sh /Users/jschoi/work/land_auction_monitor
```

다시 실행:

```bash
cd /Users/jschoi/work/land_auction_monitor
.venv/bin/streamlit run app.py
```

## 사용

1. 검색을 실행합니다.
2. 투자후보 표에서 원하는 행을 클릭합니다.
3. 팝업의 `상세정보`, `지도`, `법원 사건정보`, `원시정보` 탭을 확인합니다.
4. `법원 사건정보` 탭의 버튼을 누르면 사건상세와 매각기일내역을 추가 조회합니다.

## 지도

- 팝업 안에 Google 지도 임베드
- Google 지도, 네이버 지도, 카카오맵 외부 열기
- 주소 기반 위치이므로 지적 경계·진입로·도로접면은 입찰 전 별도로 확인해야 합니다.

## 호환성

Streamlit의 행 선택 기능을 사용합니다. 기존 환경에서 행 클릭이 동작하지 않으면 다음을 실행하십시오.

```bash
.venv/bin/python -m pip install 'streamlit>=1.36,<2'
```
