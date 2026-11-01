# 다른 저장소에서 옮기거나 참고한 것

이 저장소의 코드는 MIT 다 ([LICENSE](LICENSE)). 아래 부분은 다른 저장소에서 옮기거나 참고했고, 옮긴 부분은 원래
라이선스를 그대로 따른다.

## 1. day-trader-skill (MIT)

- 원본: https://github.com/Jakeschincariol/day-trader-skill
- 저작권: Copyright (c) 2026 Jake Schincariol
- 쓴 곳: 저장소 전체의 뼈대 (스킬 구성과 "계산은 파이썬, Claude 는 결정하지 않는다" 는 원칙). 국내 시장 규칙과
  토스증권·KRX·DART 자료로 옮겼다.
- 라이선스와 원본 저작권 표시는 [LICENSE](LICENSE) 에 그대로 있다.

## 2. anthropics/financial-services, thesis-tracker (Apache License 2.0)

- 원본: https://github.com/anthropics/financial-services
  의 `plugins/vertical-plugins/equity-research/skills/thesis-tracker/SKILL.md` (커밋 ef165e4160f2, 2026-05-21).
- 저작권: Anthropic. 원본 저장소에는 따로 적힌 저작권 줄이 없고, 루트 LICENSE 가 Apache License 2.0 이다.
  NOTICE 파일은 없다.
- 쓴 곳: `trader-kr-masters/masters.py` 의 "논지 추적 (thesis)" 부분, `trader-kr-masters/SKILL.md` 의 "논지 추적" 절.
  이 두 부분은 Apache License 2.0 을 따른다. 전문은 [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt).
- 바꾼 것:
  - 한국어로 옮겼다. 원본은 Claude 가 따르는 절차 문서이고, 여기서는 그 절차(논지 정의, 갱신 기록, 근거 점수표,
    촉매 달력, 분기마다 다시 보기)를 명령으로 만들었다 (`thesis new / add / check / show / list`).
  - 숫자로 쓸 수 있는 근거와 위험은 DART 사업보고서 값과 KRX 시가총액으로 자동 판정한다.
  - 지난 점검 뒤 나온 DART 공시를 보여 준다.
  - 매수 논지만 다룬다 (공매도 논지를 뺐다).
  - 조치(늘림·줄임·정리), 확신도, 목표가, 손절 조건은 사용자가 적는 칸으로만 두었다. 코드와 Claude 는 매수·매도나
    조치를 제안하지 않는다.
  - 아침 회의, 포트폴리오 검토, 위험 위원회용 출력 형식은 뺐다.
- 이 저장소는 Anthropic 이 만들거나 보증한 것이 아니다.

## 3. 자료와 문서 (코드를 옮기지 않음)

- 자료: 토스증권 Open API (시세, 본인 계좌 읽기. 본인 매매 목적으로만), KRX Open API 유가증권·코스닥 일별매매정보,
  OpenDART (공시 목록 list.json 과 사업보고서 접수 기록, 단일회사 전체 재무제표 fnlttSinglAcntAll.json, 배당
  alotMatter.json, 기업개황 company.json, 회사 고유번호 corpCode.xml), 한국투자증권 모의투자 API. 자료와 결과는 저장소에 올리지 않는다 (`trader-kr/` 는 .gitignore).
- 대가 기준: 벤저민 그레이엄 『현명한 투자자』 (방어적 투자자 기준), 피터 린치 『전설로 떠나는 월가의 영웅』
  (유형 분류, PEG, 배당 더한 PEG), 워런 버핏 버크셔 해서웨이 주주서한 (자기자본이익률, 유보이익 1달러 기준).
  기준값은 한국 자료에 맞춰 이 저장소가 정했고, 책의 글은 옮기지 않았다.
- 가치 규칙 (`trader-kr-invest/value.py`) 의 생각: 저PER 은 S. Basu (1977), "Investment Performance of Common Stocks
  in Relation to Their Price-Earnings Ratios", Journal of Finance. 장부가 대비 시가는 E. Fama and K. French (1992),
  "The Cross-Section of Expected Stock Returns", Journal of Finance. 가치와 모멘텀을 함께 보는 것은 C. Asness,
  T. Moskowitz and L. Pedersen (2013), "Value and Momentum Everywhere", Journal of Finance. 규칙의 꼴(30종목, 4월 말,
  순위 합)과 기준값은 이 저장소가 정했고, 논문의 글이나 코드는 옮기지 않았다.
- 에이전트 형식: Claude Code 공식 문서 (https://code.claude.com/docs/en/plugins/manifest-reference.md,
  plugins/components.md, sub-agents.md). `agents/trader-kr-analyst.md` 의 위치, 머리말, `${CLAUDE_PLUGIN_ROOT}` 를
  따랐고 문서의 글은 옮기지 않았다.

## 4. virattt/ai-hedge-fund (MIT, 참고만)

- 원본: https://github.com/virattt/ai-hedge-fund 의 `hedge_fund/signals/` (graham, buffett, lynch, munger 프롬프트).
- 쓴 곳: `trader-kr-masters` 의 대가별 관점의 틀 (예: 그레이엄의 안전마진·재무 튼튼함·이익 안정, 린치의 유형 분류와
  PEG). 프롬프트 문장이나 코드는 옮기지 않았고, 기준은 원저작(그레이엄 『현명한 투자자』, 린치 『전설로 떠나는 월가의
  영웅』, 버핏 주주서한)과 한국 자료에 맞춰 새로 썼다. 원본처럼 강세·약세 신호와 확신도를 내지 않는다.
- 원본 라이선스:

```
MIT License

Copyright (c) 2024 Virat Singh

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
