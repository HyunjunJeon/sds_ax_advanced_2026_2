# CSV 매출 집계 CLI

로컬 CSV를 읽어 지정 기간의 고객별 매출 합계를 JSON으로 출력한다. Python 표준 라이브러리만 사용한다.

```bash
python -m csv_report --input data/sales.csv --start 2026-07-01 --end 2026-07-31
python -m unittest discover -s tests -v
```

CSV 열은 `date,customer,amount,status`다. 날짜는 ISO 형식, 금액은 십진수 문자열이다.
`status=paid`인 행만 합산한다. 시작일과 종료일을 **모두 포함**하며 환불은 음수 금액으로 합산한다.
고객별 금액과 전체 합계는 소수 둘째 자리까지 문자열로 출력한다. 결과의 고객 순서는 이름순이다.
기간 밖 자료와 취소된 거래는 제외한다. 결과가 없으면 `customers={}`, `total="0.00"`이다.
시작일이 종료일보다 늦으면 오류다.

현재 버전에는 기간 경계와 관련된 결함이 있다. 테스트 실패를 읽어 재현하고 수정한다.
추가 기능의 입력·출력 계약은 요청을 받은 뒤 정한다. 기존 계약은 유지한다.
