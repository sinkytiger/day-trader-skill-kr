---
name: trader-kr-budget
description: >-
  국내 주식 수량 계산. 계획 하나를 예산과 거래당 최대 손실에 맞춰 몇 주 살지 정한다.
  수량 = 최대 손실 / (주당 위험 + 손절 때 주당 비용), 종목 한도, 주문 한도, 현금(버퍼 뺀) 중
  가장 작은 것을 1주 단위로 내림. 비용은 수수료와 매도 거래세. 지금 가격(토스증권 현재가)이
  계획 진입가보다 높으면 그 가격으로 다시 잰다. 0주, 비용 뺀 손익비 미달, 최소 주문 미만이면
  주문 없이 거절한다. 매수 1건과 그 뒤에 걸 손절·목표 한 쌍(OCO) 제안을 trader-kr/orders.json
  에 쓴다. 보내지 않는다. 사용자가 "몇 주 사", "수량 정해 줘", "얼마나 사야 돼", "포지션 크기",
  "size this trade", "how many shares" 라고 하거나 /trader-kr-timemachine 다음,
  /trader-kr-risk 전에 쓴다.
argument-hint: "[--limit [PRICE]] [--budget KRW]"
---

# trader-kr-budget

수량 계산은 한 가지를 지킨다. 손절에 닿았을 때 잃는 돈이 미리 정한 금액을 넘지 않게. 국내판은 그 금액에
수수료와 매도 거래세까지 넣는다. 위험이 가격의 1% 인 단타라면 매도 거래세 0.20% 만으로 위험의 5분의 1이다.

`${CLAUDE_SKILL_DIR}` 는 이 SKILL.md 가 있는 폴더다. macOS·리눅스에서는 `python3`.

```bash
python "${CLAUDE_SKILL_DIR}/size.py"                    # trader-kr/plan.json + trader-kr/config.json
python "${CLAUDE_SKILL_DIR}/size.py" --write            # trader-kr/orders.json 에 제안 쓰기
python "${CLAUDE_SKILL_DIR}/size.py" --limit            # 계획 진입가 지정가
python "${CLAUDE_SKILL_DIR}/size.py" --limit 72300 --write
python "${CLAUDE_SKILL_DIR}/size.py" --price 72400      # 지금 가격을 직접 주기 (토스 안 부름)
python "${CLAUDE_SKILL_DIR}/size.py" --json
```

설정 예시는 이 폴더의 `config.example.json`. `trader-kr/config.json` 으로 복사해 숫자를 바꾼다.

## 읽고 쓰는 것

| 파일 | 내용 |
| --- | --- |
| `trader-kr/plan.json` | 청산 계획 (진입, 손절, 목표, 청산 시각) |
| `trader-kr/config.json` | `budget`, `max_loss_per_trade_krw`, `max_position_pct`, `max_order_krw`, `cash_buffer_pct`, `min_order_krw`, `min_reward_risk`, `commission_rate`, `sell_tax_rate`, `blocked` |
| `trader-kr/portfolio.json` | 있으면 `cash` 가 수량 상한 (선택) |
| 토스증권 현재가 | 오늘 계획이고 지정가가 아니면 한 번 부른다 |
| 토스증권 호가창 | 오늘 계획이면 한 번 부른다 (`--book FILE` 로 대신 줄 수 있음). 스프레드, 시장가 평균 체결가, 중간가 절약, 손절 때 밀림 |
| `trader-kr/orders.json` | 주문 제안 1건: 매수(시장가 또는 지정가, 당일)와 `exit` (손절가, 목표가, 청산 시각) |

## 계산

```
주당 손실 = (진입가 - 손절가) + 진입가 x 수수료 + 손절가 x (수수료 + 매도세)
수량 = 내림(가장 작은 것)
  최대 손실   max_loss_per_trade_krw / 주당 손실
  종목 한도   budget x max_position_pct / 진입가
  주문 한도   max_order_krw / 진입가
  현금        (budget 또는 계좌 현금 - 버퍼) / (진입가 x (1 + 수수료))
```

**체결 비용.** 호가창이 있으면 (`use_book_slippage`, 기본 켬) 손절가에 닿았을 때 지금 매수 잔량 모양으로 수량만큼
팔면 손절가보다 얼마나 아래에서 평균 체결되는지 재서 주당 손실에 더하고 수량을 다시 정한다. 시장가로 사면 평균 얼마에
체결되는지, 중간가·지정가로 들어가면 최대 얼마를 아끼는지도 보여 준다. 급락 때는 잔량이 얇아져 더 밀릴 수 있다.

**비용 필터.** 손절 때 비용(수수료 + 매도세)과 예상 밀림을 합친 값이 주당 위험의 `max_cost_to_risk`(기본 30%)를 넘으면
거절한다. 손절 폭이 좁을수록 비용이 남는 몫을 먹기 때문이다.

진입가는 지정가면 지정가, 아니면 계획 진입가와 지금 가격 중 높은 쪽 (지금 가격은 호가 단위로 올림). 지정가는 호가
단위여야 한다. 지금 가격이 이미 손절가 이하면 계획이 깨진 것이라 거절한다.
손절 때 손실과 목표 때 이익은 원 미만을 버린 비용으로 다시 계산해 보여 준다.

## 절차

1. `/trader-kr-exit` 와 `/trader-kr-timemachine` 뒤에 `size.py` 를 돌린다.
2. 사용자에게 말한다: 몇 주, 얼마, 어떤 한도가 정했는지, 손절 때 손실 (비용 포함), 목표 때 이익.
3. 거절이면 이유를 그대로 말한다. 한도를 늘리자고 먼저 권하지 않는다.
4. 사용자가 받아들이면 `--write` 로 제안을 저장하고 `/trader-kr-risk` 로 넘긴다.

## 예시 (합성 자료)

예산 5,000,000원, 거래당 최대 손실 50,000원. 진입 30,450, 손절 30,100.

```
한도 (1주 단위, 가장 작은 것이 정함):
  최대 손실    50,000원 / (위험 350 + 비용 69.3)원      119주  <- 이것
  종목 한도    75% x 5,000,000원 = 3,750,000원 / 30,450  123주
  주문 한도    4,000,000원 / 30,450                      131주
수량: SYN001 119주 = 3,623,550원 (30,450 기준). 손절 때 손실 49,893원, 목표 때 이익 74,788원 (둘 다 비용 뺀 추정).
```

비용을 빼고 재면 최대 손실 한도가 142주로 늘어 종목 한도 123주가 정한다. 123주는 손절 때 비용까지
51,570원을 잃어 한도 50,000원을 넘는다.

## 지킬 것

- 1주 단위만. 국내 주식은 소수점 매수를 이 팩에서 쓰지 않는다.
- 손절은 약속이 아니다. 급락, 하한가, VI 단일가 중에는 손절가보다 낮게 체결될 수 있다. 최대 손실은 손절가에서
  체결된다고 친 값이다.
- 제안만 쓴다. 다음은 `/trader-kr-risk`.

## 안전

- 교육과 도구일 뿐 투자 권유가 아닙니다 (not financial advice). PASS 는 규칙 점검이지 추천이 아닙니다.
- 1단계에는 주문을 보내는 도구가 없습니다. 이 팩의 어떤 스크립트도 토스증권 주문·계좌 API 를 부르지 않습니다.
- 토스증권 Open API 에는 모의투자 서버가 없습니다. 나중에 주문 단계를 붙여도 기본값은 보내지 않음
  (`"send_orders": false`) 이고, 주문은 세 가지가 모두 맞을 때만 나갑니다: trader-kr-risk 가 오늘 그 주문
  그대로 통과시켰고, 사용자가 채팅에서 그 주문 하나에 yes 라고 했고, Claude 의 도구 승인 카드에서 한 번만
  허용을 눌렀을 때.
- 미수, 신용, 공매도는 쓰지 않습니다.
- 토스증권 client_id 와 client_secret 은 `~/.tossinvest/credentials.json` 또는 환경변수에 두고 채팅에 붙여 넣지 않습니다.
- 토스증권 자료는 본인 매매 목적으로만 씁니다. `trader-kr/cache/` 를 공유하거나 저장소에 올리지 않습니다.
