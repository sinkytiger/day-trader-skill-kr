# day-trader-skill-kr

[![tests](https://github.com/sinkytiger/day-trader-skill-kr/actions/workflows/tests.yml/badge.svg)](https://github.com/sinkytiger/day-trader-skill-kr/actions/workflows/tests.yml)

국내 주식(코스피·코스닥) 단타용 Claude 스킬 10종. 차트를 스캔하고, 진입 순간을 알려 주고, 손절가와 목표가를
먼저 정하고, 남은 장을 10,000번 다시 돌려 보고, 최대 손실에 맞춰 수량을 정하고, 한도를 넘는 주문을 막는다. 그리고
실제 거래를 일지에 적어 계획을 지켰는지 재고, 여러 규칙을 앞 기간에서 골라 뒤 기간에서 확인해 비용을 내고도 남는
규칙이 있는지 가린다. "오늘 장 봐 줘" 한마디면 지금 시각에 맞춰 이 단계들을 이어서 돌리고 한 장으로 보여 준다.

[Jake Schincariol 의 day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT, 미국 주식 전용)을
국내 시장 규칙과 토스증권 Open API 시세로 옮겼다. 계산은 파이썬이 하고 Claude 는 결정하지 않는다는 원본의 원칙은
그대로다. 표준 라이브러리만 쓰고, 설치할 패키지가 없다.

**주문은 한국투자증권 모의투자 계좌로만, 세 겹 승인 뒤에 낸다.** 주문을 내는 것은 `/trader-kr-broker` 하나이고,
실전 계좌와 토스증권 주문 API 는 이 저장소의 어떤 스크립트도 부르지 않는다. 기본값은 보내지 않음이다. 토스증권
계좌는 일지가 읽기만 한다 (보유 주식, 체결 내역).

> 교육과 도구일 뿐 투자 권유가 아니다 (not financial advice). 단타를 하는 사람 대부분은 돈을 잃는다.

## 스킬 10종

| 명령 | 이름 | 하는 일 |
| --- | --- | --- |
| `/trader-kr-today` | 오늘 장 한 번에 | 지금 시각에 맞춰 아래 스킬을 이어서 돌린다. 장중에는 후보마다 신호, 손절·목표, 수량, PASS / BLOCK, 재생을 한 덩어리로 |
| `/trader-kr-scanner` | 차트 스캐너 | 거래대금 상위 100 (또는 전 종목) 에서 시가 범위 돌파, 20일 신고가, 깃발, 과매도 반등을 찾는다 |
| `/trader-kr-entry` | 진입 타이머 | 5분봉이 시가 범위 고가 위에서 끝나는 순간을 알려 준다 |
| `/trader-kr-exit` | 청산 계획 | 사기 전에 손절가와 목표가를 호가 단위로 정하고 비용 뺀 손익비를 보여 준다 |
| `/trader-kr-timemachine` | 타임머신 | 지난 60 거래일 5분봉으로 남은 장을 10,000번 재생하고 같은 규칙을 지난 거래일에 다시 적용한다 |
| `/trader-kr-budget` | 수량 계산 | 수수료와 매도 거래세까지 넣은 최대 손실에 맞춰 몇 주 살지 정한다 |
| `/trader-kr-risk` | 위험 점검 | 투자경고·VI·거래정지 종목, 시간 밖 진입, 한도 초과, 비용에 비해 작은 위험을 PASS / BLOCK 으로 가른다 |
| `/trader-kr-broker` | 주문 실행 | 한국투자증권 모의투자로만, 위험 점검 PASS, 채팅의 "네", 도구 승인 카드를 모두 지난 주문을 낸다. 체결되면 목표가 지정가를 걸고, watch 가 손절가와 청산 시각을 지킨다 |
| `/trader-kr-journal` | 거래 일지 | 실제 진입·청산을 적고 늦은 진입, 손절 어김, 청산 시각 어김, 낸 비용을 숫자로 보여 준다. 토스증권에서 산 것은 계좌의 체결 내역을 읽어 적고, 보유 주식이 손절가에서 얼마나 떨어져 있는지 보여 준다 (읽기만) |
| `/trader-kr-lab` | 전략 검증 실험실 | 48가지 규칙을 앞 120 거래일에서 고르고 뒤 20 거래일에서 확인한다. 우연을 홀름 보정으로 걸러 낸다 |

원본의 나머지 넷 (뉴스, 가격 알림, 실적 요약, 의원 매매 공시) 은 아직 옮기지 않았다. 뉴스는 DART 공시, 실적 요약은
DART 재무, 의원 매매는 DART 임원·주요주주 소유보고로 바꿀 계획이다. 주문 실행은 아래 "주문 (모의투자)" 를 보라.

## 흐름

모든 결과는 프로젝트의 `trader-kr/` 폴더에 쌓이고, 다음 스킬이 앞 스킬의 파일을 읽는다.

```
today = scanner -> entry -> exit -> budget -> risk -> timemachine  (한마디로, 후보 여러 개)
scanner -> entry -> exit -> timemachine -> budget -> risk      (거래 하나, 단계별로)
journal: sync / open / close / holdings / report              (산 뒤, 판 뒤, 주말마다. sync 는 토스 체결 읽기)
lab: run                                                       (규칙을 바꾸기 전에)
broker: show -> approve -> place -> watch                      (모의투자, 주문마다 "네" 와 승인 카드)
```

## 키 없이 먼저 돌려 보기

```bash
git clone https://github.com/sinkytiger/day-trader-skill-kr.git
cd day-trader-skill-kr
python krcommon/demo.py
```

합성 종목 3개(실제 시세 아님)로 8단계를 차례로 돌리고, 끝에 같은 자료로 `/trader-kr-today` 한 번에 보기를 보여 준다. 각 스킬이 쓰는 스크립트를 오프라인 옵션으로 그대로 부르고,
돌린 명령과 출력을 단계마다 보여 준다. 5초쯤 걸리고, 결과 파일은 `trader-kr-demo/trader-kr/` 에 남는다. 네트워크를
쓰지 않고 실제 키도 읽지 않는다: 스크립트의 홈 폴더를 데모 폴더로 바꾸고 키 환경변수를 지운 채 돌린다. 플러그인으로
설치했다면 Claude 에게 "데모 돌려 줘" 라고 하면 된다.

```
== 6/8 위험 점검: 종목 상태, 시간, 한도 (/trader-kr-risk) ==
PASS  #1 매수 SYN001 합성종목 119주, 진입
          진입 30,450 (계획 진입가), 손절 30,100, 목표 31,150: 주당 위험 350원
          손절 때 손실 49,893원 (한도 50,000원), 비용 뺀 손익비 1.50 (최소 1)
...
== 8/8 전략 검증 실험실: 48개 규칙을 앞에서 고르고 뒤에서 확인 (/trader-kr-lab) ==
고른 규칙의 시험 기간 성적: 거래 36건, 비용 뺀 평균 R -0.518 (95% 구간 -0.883 ~ -0.137). 비용 뺀 기대값이
음수입니다. 이 규칙들로는 남는 게 없습니다.
```

합성 자료는 시드를 고정한 무작위 걸음이라 남는 규칙이 없어야 정상이고, 실제로 그렇게 나온다.

## 설치

Claude Code 플러그인으로:

```
/plugin marketplace add sinkytiger/day-trader-skill-kr
/plugin install day-trader-skill-kr@day-trader-skill-kr
```

2026-10-29 에 Claude Code 2.1.309 의 빈 설정에서 위 두 줄로 0.3.0 을 설치해 스킬 9개가 올라오고 설치된 사본에서 데모가 끝까지 도는 것을 확인했다 (항상 드는
토큰 약 1,180). 또는 폴더를 복사한다. 스크립트가 공통 모듈 `krcommon/` 을 옆 폴더에서 찾으므로 같이 복사해야 한다.

```bash
git clone https://github.com/sinkytiger/day-trader-skill-kr.git
cp -r day-trader-skill-kr/trader-kr-* day-trader-skill-kr/krcommon ~/.claude/skills/
```

파이썬 3.8 이상. Windows 에서는 `python`, macOS·리눅스에서는 `python3` 으로 돌린다.

## 토스증권 Open API 준비

1. 토스증권 계좌가 있어야 한다. 토스증권 PC 웹(WTS) 의 설정 > Open API 에서 클라이언트를 만들어
   `client_id` 와 `client_secret` 을 받는다.
2. 같은 메뉴의 허용 IP 관리에 이 컴퓨터의 공인 IP 를 등록한다. 등록하지 않은 IP 에서 부르면 403 으로 막힌다.
   집 인터넷의 IP 가 바뀌면 다시 등록해야 한다.
3. 키를 등록한다. 본인 터미널에서 아래 명령을 돌리고 client_id 와 client_secret 을 붙여 넣는다. 입력은 화면에
   보이지 않고, 저장소 밖 `~/.tossinvest/credentials.json` 에 저장된다. 채팅에는 붙여 넣지 않는다.

   ```bash
   python krcommon/keys.py toss
   ```

   환경변수 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` 이나, 파일 위치를 가리키는 `TOSS_CREDENTIALS` 도 된다.
   토큰은 `~/.tossinvest/token.json` 에 만료 시각과 함께 저장한다. 클라이언트당 토큰이 하나만 유효해서다.
4. 연결을 점검한다. 인증 정보, 토큰, 오늘 장 운영 정보, 1분봉 하나를 차례로 확인하고, 키 값은 찍지 않는다.

   ```bash
   python krcommon/doctor.py
   ```

5. (선택) OpenDART 키를 등록하면 그날 공시를 경고한다 (`python krcommon/keys.py dart`, 또는 `DART_API_KEY` 환경변수).
   없으면 확인하지 않았다고 말한다. 어떤 키가 어디 있는지는 `python krcommon/keys.py status` 로 본다 (값은 찍지 않는다).

6. 설정 파일을 만든다: `trader-kr-budget/config.example.json` 을 `trader-kr/config.json` 으로 복사하고 숫자를 바꾼다.
   `commission_rate` 0.00015 는 토스증권 문서의 예시 값이다. 본인 계좌 수수료율로 바꾼다.

이 팩이 부르는 토스증권 API 는 시세와 종목 정보(캔들, 현재가, 상·하한가, 종목 정보, 전체 종목, 매수 유의사항,
장 운영 정보, 랭킹)와, 일지의 `sync`·`holdings` 가 쓰는 본인 계좌 읽기 셋(계좌 목록, 보유 주식, 주문 내역)이다.
모두 GET 이고, 주문·정정·취소·조건주문은 부르지 않는다. 그룹별 초당 한도에 여유를 두고 쉬어 가며 부르고, 429 를
받으면 `Retry-After` 만큼 기다린다.

**자료 이용 범위.** 토스증권 Open API 자료는 "투자자 본인의 매매 목적" 으로만 쓸 수 있다. 받은 1분봉은
`trader-kr/cache/` 에만 쌓고, 이 폴더는 `.gitignore` 에 들어 있다. 공유하지 않는다. 그래서 이 저장소의 시험은 전부
합성 자료로 돈다.

## KRX Open API 준비 (선택, 장 전 스캔용)

토스증권 일봉(1d) 종가는 KRX+NXT 통합이라 20:00 NXT 애프터마켓 마지막 가격이다. 2026-10-28 삼성전자는 토스 일봉
종가가 245,000원, 한국거래소 종가(15:30 종가 단일가)가 242,000원이었다. 장 전 스캔의 신고가, 이동평균, RSI 는
KRX 종가로 재야 하므로, KRX Open API 키가 있으면 한국거래소 공식 일별매매정보를 쓴다.

1. [KRX Open API](https://openapi.krx.co.kr) 에 회원가입하고 로그인한다.
2. 마이페이지에서 API 인증키를 신청한다. 승인되면 키가 나온다.
3. 서비스 이용에서 주식의 "유가증권 일별매매정보" 와 "코스닥 일별매매정보" 를 각각 이용신청한다. 서비스마다 승인이 따로다.
4. 키를 등록한다. 본인 터미널에서 아래 명령을 돌리고 키를 붙여 넣으면 `~/.krx/api_key` 에 저장하고, 두 서비스를
   하나씩 불러 승인됐는지 바로 알려 준다. 환경변수 `KRX_API_KEY` 를 써도 되고, 둘 다 있으면 환경변수가 먼저다.

   ```bash
   python krcommon/keys.py krx
   ```

   저장하지 않고 지금 키의 승인만 보려면 `python krcommon/keys.py krx --check`.

한 번 부르면 그날 한 시장 전 종목이 온다. 자료는 매일 08:00 에 전 거래일 것으로 갱신되고, 키당 하루 10,000건까지다.
KRX 응답이 한 번에 몇 초씩 걸려서 날짜 여섯 개씩 동시에 받는다. 처음 60 거래일을 받는 데 86초, 그다음부터는 캐시에서
몇 초다 (2026-10-29 실측). 키가 없으면 토스 일봉을 쓰고, 종가 기준이 다르다고 경고한다. 장중 판단과 상·하한가 추정은
토스 1분봉의 KRX 종가 단일가 봉을 직접 써서 이 문제의 영향을 받지 않는다.

## 미국판에서 바꾼 것

| 항목 | 원본 (미국) | 국내판 |
| --- | --- | --- |
| 시세 | Yahoo 5분봉 60일 (비공식) | 토스증권 1분봉을 5분봉으로 묶음. 국내 1분봉은 2022-11-23 이후 제공 |
| 일봉 | Yahoo 일봉 | KRX Open API 일별매매정보 (한국거래소 공식 종가). 키가 없으면 토스 일봉과 경고 |
| 장 시간 | 09:30 ~ 16:00, 5분봉 78개 | 접속매매 09:00 ~ 15:20, 5분봉 76개. 15:20 ~ 15:30 종가 단일가는 단타 판단에 안 씀 |
| 늦게 여는 날 | 조기 폐장 목록 | 토스증권 장 운영 정보로 개장·마감 시각을 받아 진입 시간과 청산 시각을 함께 옮김 |
| 기본 시간 | 진입 09:45 ~ 15:30, 청산 15:55 | 진입 09:15 ~ 14:50, 청산 15:15 |
| 가격 | 센트 | 호가 단위 (2,000원 미만 1원 ~ 50만원 이상 1,000원). 손절은 내림, 목표는 반올림, 호가 사이 진입가는 올림 |
| 가격제한 | 없음 | 상·하한가 ±30%. 목표가가 상한가 위면 거절, 시뮬레이션 가격도 그 안으로 자름 |
| 비용 | 손익에 안 넣음 | 수수료와 매도 거래세 0.20% (2026년 매도분) 를 최대 손실과 손익비에 넣음 |
| 종목 상태 | 주식·ETF 형식 검사 | 투자경고·투자위험·단기과열·정리매매·VI 발동·거래정지·우선주·ETF 를 막음. 상태를 못 받으면 막음 |
| 데이트레이더 규칙 | 미국 PDT 선택 | 국내에 없어서 뺌 |
| 재생 보조 결과 | 없음 | 30분 묶음 재생, 120 거래일 과거 재적용과 95% 구간 |
| 사건 | 뉴스 스킬 따로 | `DART_API_KEY` 가 있으면 위험 점검과 타임머신이 그날 공시를 경고 |
| 늦은·깨진 계획 | 신선한 가격으로 손실 재기 | 같음. 그리고 현재가가 손절가 이하면 막고, 신호 뒤 15분이 지나면 경고 |
| 체결 비용 | 없음 | 토스 호가창으로 시장가 평균 체결가, 중간가 절약, 손절 때 밀림을 재서 수량과 최대 손실에 넣음 |
| 비용 필터 | 없음 | 손절 때 비용과 밀림이 주당 위험의 30% 를 넘는 거래를 막음 |
| 기록과 검증 | 없음 | 거래 일지, 워크포워드 전략 검증 실험실 |
| 주문 | Alpaca, IBKR (모의계좌 기본, 손절·목표는 브래킷 주문) | 한국투자증권 모의투자만. 목표가는 지정가로 걸고, 손절은 watch 가 지켜보다 시장가로 |

**호가 단위와 체결가.** 손절가, 목표가, 지정가는 한국거래소의 2023-01-25 표 (2,000원 미만 1원, 5,000원 미만 5원,
2만원 미만 10원, 5만원 미만 50원, 20만원 미만 100원, 50만원 미만 500원, 그 이상 1,000원) 에 맞춘다. 2026-10-29
토스증권 호가창으로 확인했다. 그런데 토스증권 1분봉의 체결가에는 호가 사이 가격이 섞여 나온다. 삼성전자는 245,250원
(500원 칸의 한가운데)에, 5원 단위 종목은 4,012원 (4,012.5원을 원 단위로 버린 값) 에 체결됐다. 최우선 매수·매도 호가의
중간에서 체결되는 중간가호가 체결이다. 2025년 3월 넥스트레이드 출범 때 한국거래소와 넥스트레이드가 함께 도입했다
([금융위원회 자료](https://www.fsc.go.kr/no010101/83953)). 그날 소형주는 체결가의 1~2%, 삼성전자는 10% 정도가 그랬다.
그래서 진입 신호 가격이 호가 사이면 바로 위 호가로 올려 위험을 잰다. `krcommon/doctor.py` 는 호가창 간격이 표와 맞는지
점검한다.

**가격제한폭.** 기준가의 30% 를 기준가 호가 단위로 버린 폭을 위아래로 똑같이 적용한다. 2026-10-29 토스증권이 준
삼성전자 상한가 314,500원, 하한가 169,500원 (기준가 242,000원) 과 맞는다.

**증권거래세.** 2026-01-01 매도분부터 코스피 0.05% 에 농어촌특별세 0.15%, 코스닥 0.20% 로 둘 다 실효 0.20% 다.

## 주문 (모의투자)

`/trader-kr-broker` 가 한국투자증권 Open API 모의투자 서버(openapivts.koreainvestment.com:29443)로만 주문한다.
토스증권 Open API 에는 모의투자 서버가 없어서(2026-10-29 문서 확인) 주문은 한국투자증권 모의투자로 연습한다. 실전 서버
주소는 이 저장소에 없고, 인증 정보가 실전 계좌라고 하면 거절한다.

주문은 원본처럼 세 겹 승인을 지킨다: trader-kr-risk 가 오늘 그 주문 그대로 통과시켰고(지문이 같음), 사용자가 채팅에서 그
주문 하나에 "네" 라고 했고(`approve` 가 답을 글자 그대로 기록, 10분 안에 한 번만), Claude 의 도구 승인 카드에서 허용을
눌렀을 때만. 그리고 `trader-kr/config.json` 의 `"send_orders"` 가 true 여야 하는데, 이 값은 사용자가 직접 바꾼다.

체결되면 일지에 적고 목표가 지정가 매도를 증권사에 건다. 손절은 증권사에 걸리지 않는다: `broker.py watch` 가 현재가를
보다가 손절가 이하이거나 청산 시각(15:15)이 되면 목표가 주문을 취소하고 시장가로 판다. watch 가 돌고 있지 않으면 손절이
지켜지지 않는다. 쓰는 API 와 거래 ID 는 한국투자증권 공식 예제(github.com/koreainvestment/open-trading-api)로 확인했다:
주문 VTTC0012U(매수)·VTTC0011U(매도), 취소 VTTC0013U, 주문체결 조회 VTTC0081R, 잔고 VTTC8434R, 현재가 FHKST01010100.

준비는 사용자가 한다: 한국투자증권 계좌, KIS Developers 모의투자 신청과 모의투자 앱키, 그리고 본인 터미널에서
`python krcommon/keys.py kis` (앱키·앱시크릿·계좌번호를 숨김 입력, `~/.kis/credentials.json`), `python krcommon/keys.py kis --check`
(잔고를 한 번 조회). 토큰은 하루 쓰고 1분에 한 번만 발급되며 발급 때 알림톡이 가서 `~/.kis/token.json` 에 저장해 다시 쓴다.

## 토스증권 계좌 읽기 (일지)

토스증권 앱에서 직접 사고판 거래를 일지에 옮겨 적는다. 주문은 사용자가 앱에서 내고, 이 팩은 계좌를 읽기만 한다.

```bash
python trader-kr-journal/journal.py sync --dry-run    # 오늘 체결을 보여 주기만 (처음 한 번 확인용)
python trader-kr-journal/journal.py sync              # 오늘 체결을 일지에 적기
python trader-kr-journal/journal.py holdings          # 보유 주식, 손절가까지 거리, 확인할 것
```

- 쓰는 API 는 셋이고 모두 GET 이다: 계좌 목록(`/api/v1/accounts`), 보유 주식(`/api/v1/holdings`), 주문
  목록(`/api/v1/orders`, 끝난 주문과 진행 중 주문). 이 경로는 `krcommon/tossacct.py` 에만 있고, 그 파일에 POST 나
  주문·정정·취소·조건주문 경로가 들어오면 `tests/test_repo.py` 가 실패한다.
- `sync` 는 산 체결을 그날 계획(`trader-kr/plan.json` 또는 `trader-kr/today/<종목>/plan.json`)과 묶고, 계획이 없으면
  설정 `stop_rule` 이 `pct:X` 일 때 그 손절가를 적는다. 판 체결로 거래를 닫고, 청산 이유는 가격으로 짐작한다.
  손익은 토스증권이 준 실제 수수료·세금으로 잰다. 같은 주문을 두 번 적지 않는다.
- 같은 날 나눠 산 것은 평균가로 합치고, 일부만 판 것은 판 만큼 떼어 닫는다. 일지에 없는 주식(전부터 갖고 있던
  주식)을 판 것은 적지 않는다. 모의투자 기록(`trader-kr-broker`)과는 섞지 않는다.
- 토스증권 명세상 주문 목록에는 Open API 가 받는 호가(지정가·시장가·장마감지정가)로 낸 주문만 나온다. 시간외
  종가 주문은 보이지 않는다. 앱에서 낸 지정가·시장가 주문이 실제로 나오는지는 처음에 `--dry-run` 으로 확인한다.
- 계좌번호는 끝 네 자리만 찍는다. 403 `forbidden` 이 나오면 이 키에 계좌 조회 권한이 없다는 뜻이다.
- 장기 보유 주식이 같은 계좌에 있으면 `--symbol 005930` 처럼 단타 종목만 고른다.

## 시험

```bash
python -m unittest discover -s tests -v
```

네트워크 없이 돈다. GitHub Actions 가 푸시마다 리눅스·Windows·macOS 의 파이썬 3.12 와 리눅스 파이썬 3.8 에서 시험과
데모를 돌린다. 시세는 `krcommon/synth.py` 가 만드는 합성 종목 SYN001 (62 거래일, 시드 고정 무작위 걸음, 마지막 날은
10:15 에 시가 범위를 돌파하도록 정한 모양) 이고, 토스증권 서버는 `tests/fake_toss.py` 가 흉내 낸다. 가짜 서버로
토큰 저장, 401 재발급, 429 대기, 403 안내, 1분봉 페이지 넘김과 캐시, 계좌 읽기(주문 목록 쪽 넘김, 권한 없음 안내,
GET 만 보내는지), 가짜 DART 공시, 그리고 스캔부터 위험 점검까지 온라인 흐름 전체를 시험한다. 시험은 실제 키 환경변수를 지우고 돈다. 스킬 문서의 예시 출력은 이 합성 자료에서 실제로 나온 값이다. 실제 시세가 아니므로 시뮬레이션 숫자에는
의미가 없다.

## 주의

- **모의일 뿐 예측이 아니다.** 원본 방식은 최근 거래일 5분봉을 서로 독립으로 다시 고른다. 그래서 같은 거래일 봉을
  30분씩 이어 붙인 묶음 재생을 나란히 보여 주고, 둘이 5%p 넘게 다르면 알린다. 과거 재적용은 120 거래일에 하고 95% 구간을
  붙인다. 구간이 본전을 포함하면 표본으로는 본전과 구별되지 않는다고 말한다. 그래도 실적, 공시, 거래정지, VI 단일가,
  상·하한가 잔량은 재료에 없으면 만들지 못한다. `DART_API_KEY` 가 있으면 그날 그 종목의 공시를 경고한다.
- **손절은 약속이 아니다.** 갭, 급락, 하한가, VI 단일가 중에는 손절가보다 나쁘게 체결되거나 체결되지 않을 수 있다.
  최대 손실은 손절가에서 체결된다고 친 값이다.
- **시세는 KRX 와 NXT 통합이다.** 토스 API 는 거래소를 골라 받을 수 없다. 접속매매 시간 밖 NXT 봉은 쓰지 않고, 일봉은
  KRX Open API 로 한국거래소 종가를 받는다. 장중 1분봉에는 같은 시간 NXT 체결도 섞여 있다.
- **세금.** 국내 상장주식 매매차익은 대주주가 아니면 보통 비과세지만 거래세는 매도마다 낸다. 세무 조언이 아니다.
- **토스증권 API 는 바뀔 수 있다.** 호출 한도와 응답 형식은 예고 없이 바뀔 수 있다. 중요한 숫자는 증권사 화면과
  대조한다.

## 파일

```
trader-kr-scanner/      SKILL.md, scan.py
trader-kr-entry/        SKILL.md, entry.py
trader-kr-exit/         SKILL.md, exit.py
trader-kr-timemachine/  SKILL.md, timemachine.py
trader-kr-budget/       SKILL.md, size.py, config.example.json
trader-kr-risk/         SKILL.md, check.py
trader-kr-journal/      SKILL.md, journal.py
trader-kr-lab/          SKILL.md, lab.py
trader-kr-today/        SKILL.md, today.py (다른 스킬을 시각에 맞춰 이어서 부름)
trader-kr-broker/       SKILL.md, broker.py (한국투자증권 모의투자 주문, 세 겹 승인)
krcommon/               krx.py (시장 규칙), toss.py (토스증권 시세), tossacct.py (토스증권 계좌 읽기, GET 만),
                        bars.py (1분봉 캐시, 5분봉), data.py,
                        dart.py (그날 공시), krxdata.py (KRX 공식 일봉), kis.py (한국투자증권 모의투자), keys.py (키 등록), liquidity.py (호가창),
                        doctor.py (연결 점검), demo.py (키 없이 8단계), synth.py (합성 1분봉)
tests/                  unittest, 가짜 토스증권 서버
.github/workflows/      tests.yml (시험과 데모, 키 없음)
```

## In English

A Korean-market port of [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT): ten Claude skills
for KOSPI/KOSDAQ day trades (scanner, entry timer, exit planner, Monte Carlo time machine, position sizing, risk check,
a trade journal that measures plan adherence, a walk-forward strategy lab with Holm-corrected tests, and a
one-call "today" skill that runs the right steps for the time of day and prints one card per candidate, and an order
skill that trades only on a Korea Investment & Securities paper account after the same three approvals as the original),
reading Toss Securities Open API market data. It adds KRX session hours (continuous trading 09:00 to 15:20), tick sizes,
the ±30% price limits, transaction tax and fees in every loss and reward:risk figure, and blocks on exchange warnings,
volatility interruptions and trading halts. Orders go only to the KIS paper-trading server, after a risk-check PASS, a "yes" in chat and a tool approval; no script calls a live account or Toss order endpoints. The journal can read the user's own Toss account (holdings and order history, GET only) to log fills placed in the Toss app. Tests run
offline on synthetic data, because Toss data may only be used for the account holder's own trading.
To see all eight steps without any keys, run `python krcommon/demo.py` (synthetic data, no network, about 5 seconds).

MIT. 원본 저작권 표시는 [LICENSE](LICENSE) 에 그대로 있다.
