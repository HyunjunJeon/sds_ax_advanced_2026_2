"""02. 적재 — idempotent 원문 업로드와 manifest 검증.

모든 문서를 업로드 전에 서버 원문과 비교해 재사용한다. catalog의 fingerprint가 같으면
같은 target_uri로 향하므로 재실행은 스토리지를 늘리지 않는다. 적재 후 원문 leaf가
하나인지, 서버 원문이 로컬과 같은지 검증하고 manifest에 remote_hash와 검증용 snapshot을
남긴다. manifest는 서버 주소와 완료 상태를 기억해서, 다른 서버·중단된 적재를 조용히
재사용하지 않는다. --expect-reused는 재실행 시 전부 재사용되는지 확인하는 회귀 플래그다.

관찰:
- 재실행 시 출력이 reused=문서 수와 같아지는지, outputs/manifests/business.json의
  complete=true와 server_url을 확인한다.
- 문서를 수정하고 다시 적재하면 fingerprint가 바뀌어 새 target_uri로 적재된다.

심화 연결:
- 12_refresh_challenge.py: 턴 사이에 문서가 갱신됐을 때 세션이 이전 인용을 어떻게
  다뤄야 하는지(이 파일은 적재 자체의 멱등성만 다룬다).

실패 상황(이 단계가 지켜야 할 것):
- 부분 실패 후 재실행이 이전 원문을 검증 없이 덮어쓰거나 이전 manifest를 재사용하면
  이후 인용의 원문 무결성 근거가 사라진다.
"""
import argparse
from day02.ingestion.pipeline import ingest
from day02.openviking.client import VikingClient
from day02.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-reused", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    with VikingClient.configured(settings) as client:
        result = ingest(settings, client, settings.root / "data/business/catalog.json")
    if args.expect_reused:
        assert result["reused"] == len(result["documents"]), "기존 원문이 모두 재사용되지 않았습니다."
    print(f"적재 완료: documents={len(result['documents'])}, reused={result['reused']}")


if __name__ == "__main__":
    main()
