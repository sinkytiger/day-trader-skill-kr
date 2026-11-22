---
name: trader-kr-alerts
description: >-
  국내 주식 가격 알림. 청산 계획에서 손절가, 목표가, 정리 시각 알림 셋을 한 번에 만들고, 지정한 가격 위·아래, 기준가
  (보통 전일 종가) 대비 몇 % 움직임, 아무 시각, 개장 알림도 건다. 각 알림은 한 번만 울린다 (가격은 되돌아가면 다시
  울리게 할 수 있다). 장 시간은 토스증권 장 운영 정보로 보고, 개장이 늦은 날에는 오후 알림을 마감에 맞춰 옮길 수
  있다. 시세는 토스증권 현재가다. 알림은 정보이고 주문을 내지 않는다. 사용자가 "알림 걸어 줘", "손절가 오면 알려
  줘", "목표가 알림", "정리 시각 알려 줘", "3% 빠지면 알려 줘", "장 열렸어?", "오늘 장 몇 시까지야", "현재가",
  "alert me when", "watch my stop" 이라고 하거나 /trader-kr-exit 가 계획을 쓴 바로 뒤에 쓴다.
argument-hint: "clock | add --from-plan | add --symbol 005930 --below 238000 | check | quote 005930 | list"
---

# trader-kr-alerts

가격 알림은 사람이 차트를 계속 보지 않아도 되게 한다. 알림은 정보이지 주문이 아니다. 손절 알림이 울려도 이 스킬은
아무것도 팔지 않는다.

모의투자 주문(`/trader-kr-broker`)은 `broker.py watch` 가 5초마다 손절가와 청산 시각을 실제로 지킨다. 이 스킬은
토스증권 앱에서 직접 산 거래, watch 를 켜지 않은 거래, 또는 "15:10 에 정리 준비" 같은 알림에 쓴다. 원본(미국판
trader-alerts)은 NYSE 시계와 Yahoo 시세를 썼고, 국내판은 토스증권 장 운영 정보와 현재가를 쓴다.

`${CLAUDE_SKILL_DIR}` 는 이 SKILL.md 가 있는 폴더다. macOS·리눅스에서는 `python3`.

```bash
python "${CLAUDE_SKILL_DIR}/alerts.py" clock                                 # 장 상태, 오늘 장 시간, 다음 개장
python "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                       # 계획의 손절, 목표, 정리 시각
python "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol 005930 --below 238000    # --above, --stop, --target 도
python "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol 005930 --pct 3 --direction down
python "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "정리 준비" --before-close
python "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python "${CLAUDE_SKILL_DIR}/alerts.py" check                                 # 울리면 종료 코드 0, 아니면 1
python "${CLAUDE_SKILL_DIR}/alerts.py" quote 005930 000660
python "${CLAUDE_SKILL_DIR}/alerts.py" list
python "${CLAUDE_SKILL_DIR}/alerts.py" reset 005930-below-238000             # 다시 울릴 수 있게
python "${CLAUDE_SKILL_DIR}/alerts.py" remove 005930-below-238000
```

`--today` 는 오늘만, `--rearm` 은 가격이 수준 밖으로 되돌아가면 다시 울림, `--now "2026-10-28 15:15"` 는 시계 고정
(한국 시간), `--no-network` 는 토스증권을 부르지 않음 (장 시간은 캐시나 평일 기본값), `check --any-time` 은 정규장
밖에도 가격 알림을 본다.

## 읽고 쓰는 것

| 파일 | 무엇 |
| --- | --- |
| `trader-kr/plan.json` | 청산 계획 (trader-kr-exit). `add --from-plan` 이 종목, 날짜, 손절가, 목표가, `flat_by` 를 읽는다 |
| `trader-kr/alerts.json` | 알림 규칙 |
| `trader-kr/state/alerts.json` | 어느 알림이 언제 울렸는지, 그날 종목별 기준가 |
| `trader-kr/cache/calendar.json` | 토스증권 장 운영 정보 (다른 스킬과 같이 씀) |

## 알림 종류

| 종류 | 울릴 때 | 다시 울림 |
| --- | --- | --- |
| `above` / `target` | 현재가가 그 가격 이상 | `reset` 뒤, 또는 `--rearm` 이면 가격이 그 아래로 돌아간 뒤 |
| `below` / `stop` | 현재가가 그 가격 이하 | 같음 |
| `pct_move` | 기준가 대비 `pct` % 이상 움직임 (`--direction up/down` 이면 그 방향만) | 하루 한 번 |
| `time` | 거래일에 한국 시간이 그 시각을 지남 | 하루 한 번 |
| `opening_bell` | 정규장이 열려 있음 | 하루 한 번 |

날짜가 있는 알림은 그날만 본다. `--from-plan` 알림은 계획의 날짜를 달아서, 어제 손절가가 오늘 울리지 않는다. 같은
계획으로 다시 더하면 그 종목 그날의 예전 계획 알림을 바꾼다 (계획을 다시 짜면 손절가가 바뀐다).

## 장 시간과 시세

- **장 시간.** 토스증권 장 운영 정보로 개장, 접속매매 끝, 마감을 본다. 받지 못하면 평일 기본값(09:00, 15:20, 15:30)을
  쓰고 휴장 여부를 확인하지 않았다고 말한다. 개장이 늦은 날(수능일 등) 에는 `--before-close` 알림을 접속매매 끝에
  맞춰 옮긴다. 계획의 정리 시각은 `/trader-kr-exit` 가 그날 장 시간으로 이미 정했으므로 옮기지 않는다.
- **시세.** 토스증권 현재가다. 가격 알림은 정규장(개장부터 마감까지)에만 본다. 정규장 밖의 현재가에는 넥스트레이드
  체결이 섞일 수 있다. 점검과 점검 사이에 가격이 수준을 지나갔다 돌아올 수 있으니 실시간 감시가 아니다. 시세 시각이
  15분보다 오래됐으면 알림에 그렇게 적는다.
- **기준가.** 토스증권 상·하한가의 가운데다. 상·하한가는 기준가에서 같은 폭만큼 위아래로 정해지므로 가운데가
  기준가다. README 의 2026-10-29 삼성전자 예 (상한가 314,500원, 하한가 169,500원, 기준가 242,000원) 와 맞는다.
  기준가는 보통 전일 종가이고, 분할·병합·권리락이 있으면 조정된 값이다.

## 단계

1. `/trader-kr-exit` 가 계획을 쓰면 바로 `add --from-plan` 을 돌리고 세 알림(손절가, 목표가, 정리 시각)을 사용자에게
   되읽어 준다: 종목, 종류, 가격이나 시각, 날짜.
2. 거래가 열려 있는 동안 몇 분마다 `check` 를 돌린다 (`/loop 5m /trader-kr-alerts` 나 예약 작업).
3. 울린 것을 한 줄씩 사실로 말한다. 손절·목표 알림은 모의투자라면 `broker.py status` 와 거래 일지로 체결을 확인하는
   신호다. 정리 시각 알림은 열려 있는 거래를 계획대로 정리할 시각이라는 뜻이고, 모의투자 정리는 `broker.py flatten`
   이 매도 주문을 만든 뒤 `/trader-kr-risk` 와 사용자의 yes 를 거친다. 사용자 대신 행동하지 않는다.

## 예: 합성 종목으로 돌린 것

데모와 같은 합성 종목(실제 시세 아님), 계획은 손절 30,100원, 목표 31,150원, 정리 15:15.

```
$ alerts.py add --from-plan
알림을 더했습니다 (trader-kr/alerts.json):
  SYN001-plan-stop         SYN001 합성종목 손절가 30,100원 이하, 2026-10-28 하루
  SYN001-plan-target       SYN001 합성종목 목표가 31,150원 이상, 2026-10-28 하루
  SYN001-plan-flat-by      SYN001 합성종목 정리 시각 15:15, 2026-10-28 하루

$ alerts.py check            (10:16, 현재가 30,450원)
2026-10-28 (수) 10:16 한국 시간, 접속매매 중 (15:20 까지).
울린 것 없음. 오늘 보는 알림 3개, SYN001 30,450원.

$ alerts.py check            (10:20, 현재가 30,100원)
ALERT 손절 SYN001 합성종목: 현재가 30,100원이 손절가 30,100원 이하입니다 (10:16 시세). 계획한 손절가입니다.
모의투자 주문이면 broker.py watch 가 지킵니다. 직접 산 거래면 계획대로 할지 정하세요.

$ alerts.py check            (15:15)
ALERT 정리 시각 15:15: SYN001 이 아직 열려 있으면 계획대로 정리할 시각입니다. 접속매매는 15:20 에 끝납니다.
모의투자는 broker.py flatten 이 매도 주문을 만들고, 위험 점검과 사용자의 yes 를 거칩니다.
```

각 알림은 한 번 울린다. 다음 날에는 이 계획의 알림을 보지 않는다.

## 하지 않는 것

주문을 내거나, 취소하거나, 바꾸지 않는다. 시세를 실시간이라고 하지 않는다. 가격이 어디로 갈지 말하지 않는다.
알림 하나로 사거나 팔라고 하지 않는다.

## 안전

- 교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice). 알림은 규칙에 맞는 사실이지 추천이 아닙니다.
- 주문은 trader-kr-broker 만 냅니다. 한국투자증권 모의투자 계좌로만 보내고, trader-kr-risk 가 그 주문을 통과시키고,
  사용자가 채팅에서 그 주문 하나에 yes 라고 하고, Claude 의 도구 승인 카드에서 한 번만 허용을 눌렀을 때만 나갑니다.
  이 스킬은 토스증권 주문 API 를 부르지 않습니다.
- 토스증권 client_id 와 client_secret 은 `~/.tossinvest/credentials.json` 또는 환경변수에 두고 채팅에 붙여 넣지 않습니다.
- 토스증권 시세는 본인 매매 목적으로만 씁니다. `trader-kr/` 의 기록을 공유하거나 저장소에 올리지 않습니다.
