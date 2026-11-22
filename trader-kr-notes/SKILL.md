---
name: trader-kr-notes
description: >-
  시장 노트 자료 만들기. 24개 나라 ETF(iShares)·원자재·국내 상장 ETF 비교, 미국 관심 종목 주간 점검(Yahoo 수정 종가,
  SEC 새 공시, 최근 4개 분기 주당순이익으로 계산한 PER), 공부용 회사 20곳의 5년 재무제표와 지표(미국 SEC, 국내 DART,
  네이버 연간 실적과 대조), 공유용 공부 노트와 주식 기초편 예시 숫자(코스피 보통주 올해 수익률 분포, KODEX 200,
  S&P500 큰 하락 구간)를 JSON 으로 만들고, 결과를 텔레그램으로 나에게 보낸다. 하나라도 못 읽으면 멈춘다. 사라·팔라는
  말은 하지 않는다. 사용자가 "시장 노트 갱신", "세계 시장 비교", "미국 관심 종목 점검", "재무제표 공부 자료",
  "공부 노트", "주식 기초", "텔레그램 알림", "market note", "watchlist check" 라고 할 때 쓴다.
argument-hint: "markets | us-watch | study | share | basics | tg setup|status|send"
---

# trader-kr-notes

대시보드나 공부 노트에 올릴 숫자를 매주 같은 방식으로 다시 만드는 스킬이다. 숫자를 손으로 옮기지 않고, 출처마다
기준일을 남기고, 하나라도 못 읽으면 지난주 숫자를 그대로 두고 멈춘다. 대시보드에 올리는 일은 이 스크립트가 하지 않는다.
결과 JSON 을 Claude 의 대시보드(Data 아티팩트)나 다른 곳에 올리는 것은 사용자가 정한다.

`${CLAUDE_SKILL_DIR}` 는 이 SKILL.md 가 있는 폴더다. macOS·리눅스에서는 `python3`.

```bash
python "${CLAUDE_SKILL_DIR}/notes.py" markets        # 24개 나라 ETF, 원자재 5개, 국내 상장 ETF 22개
python "${CLAUDE_SKILL_DIR}/notes.py" us-watch       # 미국 관심 종목 (trader-kr/notes/us-watchlist.json)
python "${CLAUDE_SKILL_DIR}/notes.py" study          # 공부용 회사 20곳 재무제표 5년 (us-watch 다음에)
python "${CLAUDE_SKILL_DIR}/notes.py" share          # 공유용 공부 노트 묶음 (markets, study 다음에)
python "${CLAUDE_SKILL_DIR}/notes.py" basics         # 주식 기초편 예시 숫자 (share 다음에)
python "${CLAUDE_SKILL_DIR}/notes.py" tg setup       # 텔레그램 봇 연결 (본인 터미널에서만)
python "${CLAUDE_SKILL_DIR}/notes.py" tg send < trader-kr/notes/us-watch/out/message.txt
```

## 단계와 결과

| 단계 | 읽는 곳 | 쓰는 곳 (`trader-kr/notes/`) | 멈추는 경우 |
| --- | --- | --- | --- |
| `markets` | iShares 펀드 목록과 상품 페이지(PER·PBR), Yahoo 시세(원·달러, ETF, 원자재 선물), 네이버 ETF 분석 | `markets/out/` markets, commodities, domestic, basis, message.txt | 24개 나라, PER 24개, 원자재 5개, 국내 ETF 22개 중 하나라도 빠짐 |
| `us-watch` | Yahoo 수정 종가, SEC submissions·companyfacts | `us-watch/out/` watch.json, message.txt, last.json | 종목 하나라도 시세·주당순이익을 못 읽음, 주당순이익 기간이 200일 넘게 오래됨 |
| `study` | SEC companyfacts, DART 사업보고서와 최근 분기보고서, KRX 일별 시가총액, 네이버 시세·연간 실적 | `study/out/` study_firms, study_years, study_check | 회사 20곳 × 5년 중 빠진 값, 자산 ≠ 부채 + 자본 (0.5% 넘게), 네이버 연간 실적과 어긋남 |
| `share` | 위 두 단계 결과, Yahoo QUAL·IVV | `share/` markets, etfs, firms, years, qual_years, basis | 앞 단계 결과가 오래됨(2일, 공부 자료는 1일), 건수 이상 |
| `basics` | KRX 일별매매정보 캐시, 네이버 KODEX 200 일별 시세, Yahoo IVV | `basics/` stats, kospi_dist, kodex_daily, ivv_monthly, ivv_drawdowns | 코스피 보통주가 500개 밑, 분포 칸 합이 종목 수와 다름, share 결과가 1일 넘게 오래됨 |

각 단계는 `report.json` 에 `ok`, 건수, 빠진 것을 적는다. 멈추면 종료 코드 2 이고 이전 결과 파일은 그대로 둔다.

## 계산 방법

- 원화 수익률은 달러 수익률에 원·달러 변화를 곱한다. 5년·10년 연평균은 iShares 의 기준가(배당 재투자) 수익률이다.
- 최근 4개 분기 = 마지막 연간 + 그 뒤 누적 분기 - 1년 전 같은 누적 분기. 미국은 희석, 한국은 기본 주당순이익.
- 국내 각 연도는 그 연도를 당기·전기·전전기로 담은 가장 최근 사업보고서의 값이다. 처음 보고서와 0.2% 넘게
  다르면 `restated` 로 표시한다 (분할·사업 매각 등으로 나중에 고쳐 쓴 숫자).
- 주당순이익 합계 줄이 없고 계속영업·중단영업으로만 공시한 회사는 둘을 더한다. 주식 종류별로만 공시한 미국 회사(예:
  비자)는 연간·분기 보고서 원문(inline XBRL)에서 A주 값을 읽는다.
- 공부용 국내 회사는 KRX 마지막 날의 코스피 보통주 시가총액 순으로, 업종 코드 64~66(금융·보험·지주)을 뺀 위 10곳이다.
- 코스피 보통주 올해 수익률은 KRX 전일 대비로 이어 붙인 조정 가격이다 (액면분할 등 반영, 배당 제외). 작년 말과
  마지막 날에 모두 상장된 종목만 센다.

## 텔레그램

`tg setup` 은 본인 터미널에서 BotFather 봇 토큰을 숨김 입력으로 받아 `~/.telegram/bot.json` 에 저장하고, 봇에게
1:1 로 말을 건 채팅 하나만 받는 곳으로 정한다. 단체방은 받지 않는다. `tg send` 는 표준입력의 글을 꾸밈 없이 보내고
3,800자마다 나눈다. 설정이 없으면 보내지 않고 종료 코드 3 이다. 토큰 값은 화면에 찍지 않는다.

## 예약 작업과 함께 쓰기

매주 토요일에 `markets -> us-watch -> study -> share -> basics` 순서로 돌리고, 단계마다 `report.json` 의 `ok` 와
건수를 확인한 뒤에만 결과를 대시보드에 올리게 하면 된다. 멈춘 주에는 이유 한 줄만 텔레그램으로 보낸다.

## 하지 않는 것

- 주식이 싸다, 비싸다, 사라, 팔라고 말하지 않는다. 관심 종목 예시 파일은 추천이 아니다.
- 규칙이 고른 종목이나 개인 연구 결과를 공유용 묶음에 넣지 않는다.
- 토스증권 자료를 읽지도 올리지도 않는다. 주문 API 를 부르지 않는다.
- 못 읽은 숫자를 다른 자료로 채우지 않는다.

## 안전

- 교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice).
- 이 스킬은 공개 자료를 읽기만 합니다. 주문은 trader-kr-broker 만 냅니다. 한국투자증권 모의투자 계좌로만 보내고,
  trader-kr-risk 가 그 주문을 통과시키고, 사용자가 채팅에서 그 주문 하나에 yes 라고 하고, Claude 의 도구 승인
  카드에서 한 번만 허용을 눌렀을 때만 나갑니다.
- DART 키는 환경변수 `DART_API_KEY` 나 `python krcommon/keys.py dart` 로 등록한 파일에 두고, 텔레그램 봇 토큰은
  `tg setup` 으로만 저장합니다. 둘 다 채팅에 붙여 넣지 않습니다.
- iShares, Yahoo, 네이버 자료는 각 사이트의 이용 조건 안에서 본인 공부용으로 씁니다. 결과를 다른 사람과 나눌 때는
  그 조건을 먼저 확인합니다.
