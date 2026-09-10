"""02. 사람이 검수한 Scenario를 실행 데이터로 동결한다. 모델·외부 호출 없음."""

from business_lab.authoring import freeze, read_workbook
from business_lab.dataset import load_fixtures
from course_config import DATASET, DRAFT_XLSX, REFINED_XLSX, SCENARIO_IDS

USE_REFINED = False  # 01b의 개선 초안을 검수했다면 True. 사용할 입력을 명시적으로 선택한다.


def main():
    source = REFINED_XLSX if USE_REFINED else DRAFT_XLSX
    drafts = read_workbook(source)
    print(f"동결할 초안: {source}")
    data = freeze(drafts, load_fixtures(), DATASET, scenario_ids=SCENARIO_IDS)
    print(f"동결 데이터: {DATASET}\n버전: {data['snapshot_hash']}")
    print("실행 조건별 문항 수:", data["coverage"])
    print("이번 실험에 선택하지 않은 초안:", data["not_selected_ids"])
    print("미검수 문항을 승인으로 바꾸지 않습니다. 일부 문항 실행을 전체 업무 성능으로 보고하지 마세요.")


if __name__ == "__main__":
    main()
