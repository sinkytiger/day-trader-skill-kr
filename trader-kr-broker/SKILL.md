---
name: trader-kr-broker
description: >-
  국내 주식 주문 실행. 한국투자증권 모의투자 계좌로만, 세 겹 승인 뒤에 주문한다. trader-kr-risk 가 오늘 그
  주문 그대로 PASS 했고, 사용자가 채팅에서 그 주문 하나에 "네" 라고 답했고, Claude 의 도구 승인 카드에서
  허용했을 때만 보낸다. 체결되면 거래 일지에 적고 목표가 지정가 매도를 걸며, watch 가 손절가와 청산 시각을
  지켜보다 시장가로 판다. 모의투자 잔고와 오늘 주문을 보여 준다. 사용자가 "주문 넣어 줘", "사 줘", "모의투자로
  넣어", "보내 줘", "팔아 줘", "정리해 줘", "잔고 보여 줘", "모의투자 연결", "place the order", "flatten"
  이라고 하거나 /trader-kr-risk 가 통과한 뒤 쓴다. 실전 계좌는 쓰지 않는다.
argument-hint: "show | approve --index N --reply \"답 그대로\" | place --index N | watch | status | flatten 종목 --reply \"답\""
---

# trader-kr-broker

이 팩에서 주문으로 이어지는 스킬은 이것 하나다. 그래서 가장 엄격하다. 주문은 매번, 주문마다 세 문을 지난다.

1. **trader-kr-risk 가 오늘 이 주문 그대로 PASS 했다** (지문이 같다. 수량이나 가격이 바뀌면 다시 점검).
2. **사용자가 채팅에서 이 주문 하나에 "네"라고 답했다.** `approve` 가 그 답을 글자 그대로 기록한다.
3. **사용자가 Claude 의 도구 승인 카드에서 허용했다.** `place` 명령을 돌릴 때 나오는 카드다.

그리고 `trader-kr/config.json` 의 `"send_orders"` 가 `true` 여야 한다. 이 값은 사용자가 직접 바꾼다.
Claude 가 대신 바꾸지 않는다.

`${CLAUDE_SKILL_DIR}` 는 이 SKILL.md 가 있는 폴더다. macOS·리눅스에서는 `python3`.

```bash
python "${CLAUDE_SKILL_DIR}/broker.py" show                                  # 주문표, READY 인지
python "${CLAUDE_SKILL_DIR}/broker.py" approve --index 1 --reply "네"         # 사용자의 답 그대로
python "${CLAUDE_SKILL_DIR}/broker.py" place --index 1                        # 모의투자로 보냄
python "${CLAUDE_SKILL_DIR}/broker.py" watch                                  # 손절가·청산 시각 지키기
python "${CLAUDE_SKILL_DIR}/broker.py" status                                 # 잔고와 오늘 주문 (조회만)
python "${CLAUDE_SKILL_DIR}/broker.py" flatten 005930 --reply "네"            # 보유분 시장가 매도
python "${CLAUDE_SKILL_DIR}/broker.py" log                                    # 오늘 기록
```

`/trader-kr-today` 의 후보로 주문할 때는 그 후보의 파일을 준다:
`--orders trader-kr/today/<종목>/orders.json --risk trader-kr/today/<종목>/risk.json`.

## 준비 (한 번, 사용자가 직접)

1. 한국투자증권 계좌를 만들고 KIS Developers(apiportal.koreainvestment.com)에서 모의투자를 신청한다.
2. 모의투자용 앱키와 앱시크릿을 발급받는다. 실전 앱키와 다르다.
3. 본인 터미널에서 `python krcommon/keys.py kis` 를 돌려 앱키, 앱시크릿, 모의투자 계좌번호를 붙여 넣는다.
   화면에 보이지 않고 `~/.kis/credentials.json` 에 저장된다. **키는 채팅에 붙여 넣지 않는다.**
4. `python krcommon/keys.py kis --check` 로 잔고를 한 번 조회해 확인한다. 토큰을 처음 받을 때 한국투자증권
   알림톡이 간다. 토큰은 하루 쓰고 1분에 한 번만 발급되어 `~/.kis/token.json` 에 저장해 다시 쓴다.
5. 보내도 된다고 정했으면 사용자가 `trader-kr/config.json` 에 `"send_orders": true` 를 넣는다.

## 읽고 쓰는 것

| 파일 | 내용 |
| --- | --- |
| `trader-kr/orders.json` | 수량 계산이 쓴 주문 제안 |
| `trader-kr/state/risk.json` | 위험 점검 결과. 오늘 PASS 이고 지문이 같은 주문만 READY |
| `trader-kr/state/broker.json` | yes 기록, 보낸 주문, 포지션. 위험 점검이 이것으로 하루 거래 수와 손실을 센다 |
| `trader-kr/journal.jsonl` | 체결되면 진입을, 나오면 청산을 적는다 |
| 한국투자증권 모의투자 API | 주문, 취소, 주문체결 조회, 잔고, 현재가 |

## 절차: 진입

1. `broker.py show`. READY 가 아닌 주문은 `/trader-kr-risk` 로 돌려보낸다.
2. READY 주문 **하나**의 주문표를 그대로 보여 주고 "이 주문을 모의투자로 넣을까요? 네 / 아니오" 라고 묻는다.
   한 번에 하나만 묻는다. "모두 넣을까요?" 는 묻지 않는다.
3. 사용자의 답을 고치지 말고 그대로 `broker.py approve --index N --reply "<답>"` 에 넣는다.
   "기록하지 않았습니다" 가 나오면 이유를 말하고 다시 묻는다. "좋아", "ok", "ㅇㅇ", "네 모두", "네 근데",
   다른 종목이나 다른 수량을 말한 답은 yes 가 아니다. 답을 yes 로 바꿔 넣지 않는다.
4. "yes 를 기록했습니다" 뒤 10분 안에 `broker.py place --index N`. 도구 승인 카드가 한 번 더 묻는다.
5. 결과를 그대로 말한다: 주문번호, 체결 수량과 평균가, 걸어 둔 목표가 주문.

## 절차: 들고 있는 동안

- 체결되면 목표가 지정가 매도가 한국투자증권에 걸린다.
- **손절은 증권사에 걸리지 않는다.** `broker.py watch` 가 5초마다 현재가를 보고 손절가 이하면 목표가 주문을
  취소하고 시장가로 판다. 청산 시각(기본 15:15)이 되면 같은 방법으로 판다. watch 를 켜 두지 않으면 손절이
  지켜지지 않는다고 사용자에게 분명히 말한다. Claude 는 watch 를 백그라운드로 돌려 둘 수 있다.
- 사용자가 직접 정리하겠다고 하면 `flatten 종목 --reply "<답>"` (그 답도 "네" 로 시작해야 한다).
- 나온 거래는 일지에 적힌다. 끝나면 `/trader-kr-journal report`.

## 예시 (가짜 모의투자 서버)

```
주문 1 / 1  한국투자증권 모의투자
  매수 SYN001 합성종목 119주, 시장가, 당일
  체결되면: 목표가 31,150 지정가 매도를 증권사에 겁니다
  손절가 30,100, 청산 시각 15:15: broker.py watch 가 돌고 있을 때만 시장가로 팝니다
  손절 때 손실 약 49,893원 (빠른 장에서는 더 나쁘게 체결될 수 있음)
  READY: 위험 점검 PASS (2026-10-28 10:16)
```

사용자가 "네" 라고 답하면 기록하고 보낸다:

```
모의투자로 보냈습니다: 매수 SYN001 119주 시장가, 주문번호 0000001000.
체결: SYN001 119주, 평균 30,500.
기록했습니다: 20261028-SYN001-1  SYN001 119주, 진입 30,500 (10:16), 손절 30,100.
나오면: journal.py close 20261028-SYN001-1 --exit-price 가격 --reason stop|target|flat|manual
목표가 31,150 에 119주 지정가 매도를 걸었습니다 (주문번호 0000001001).
손절가 30,100, 청산 시각 15:15: broker.py watch 가 지킵니다. watch 를 켜 두세요.
```

## 하지 않는 것

- 세 문을 다 지나지 않은 주문, 한 번의 yes 로 두 주문을 보내지 않는다. yes 는 10분 안에 한 번만 쓴다.
- 예약 작업이나 백그라운드에서 새 매수를 내지 않는다. watch 는 이미 yes 를 받은 포지션을 정리하는 매도만 한다.
- 실전 계좌, 미수, 신용, 공매도를 쓰지 않는다. 실전 서버 주소는 이 저장소에 없다.
- `send_orders` 를 대신 true 로 바꾸지 않는다. 키를 묻거나 읽거나 되풀이하지 않는다.
- 체결 가격을 약속하지 않는다. 시장가와 손절은 그때 시장이 주는 가격에 체결된다.

## 안전

- 교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice). PASS 는 규칙 점검이지 추천이 아닙니다.
- 주문은 이 스킬만 냅니다. 한국투자증권 모의투자 계좌로만 보내고, 이 팩의 어떤 스크립트도 토스증권
  주문·계좌 API 나 실전 계좌를 부르지 않습니다.
- 기본값은 보내지 않음 (`"send_orders": false`) 이고, 바꾸는 것은 사용자입니다. 주문은 세 가지가 모두 맞을 때만
  나갑니다: trader-kr-risk 가 오늘 그 주문 그대로 통과시켰고, 사용자가 채팅에서 그 주문 하나에 yes 라고 했고,
  Claude 의 도구 승인 카드에서 한 번만 허용을 눌렀을 때.
- 한국투자증권 앱키와 앱시크릿은 `~/.kis/credentials.json` 에 두고 채팅에 붙여 넣지 않습니다.
  토스증권 client_id 와 client_secret 도 채팅에 붙여 넣지 않습니다.
