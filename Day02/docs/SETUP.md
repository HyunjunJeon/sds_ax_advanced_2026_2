# 설치와 운영

## 환경

Day02 앱과 OpenViking 서버는 각각 Python 3.13 환경과 별도 uv.lock을 사용한다.
상위 과정의 Python 3.14 환경은 변경하지 않는다.

| 구성 | 기준 |
|---|---|
| Python | 3.13 |
| DeepAgents | 0.7.13 |
| OpenViking | 0.4.16 |
| rhwp CLI | 0.8.6, 공식 아카이브 SHA256 검증 |
| 기본 답변 모델 | `google/gemini-3.8-flash` (`.env`의 `DAY02_MODEL`로 변경) |
| 서버 문서 처리·시각 확인 모델 | `google/gemini-3.8-flash` |
| 임베딩 | `qwen/qwen3-embedding-8b`, 4096차원 |

모델은 `.env`의 `DAY02_MODEL`, `DAY02_VISION_MODEL`, `DAY02_EMBEDDING_MODEL`로 지정한다.
임베딩 모델/차원이 이미 초기화한 저장소와 다르면 init은 거부한다. 다른 임베딩 설정은 별도 작업 폴더와 새 저장소에서 검증한다.

## OS별 범위

OpenViking 공식 Quick Start는 Windows를 지원 OS로 표기하지만, Windows 전용 설치·운영 가이드는 공식 문서에서 확인하지 못했다. Windows 표기를 실기동 검증으로 해석하지 않는다.

실제 실행 검증은 macOS Apple Silicon에서 수행했다. Windows/Linux의 서버 실기동을 이 Mac에서 검증했다고 표시하지 않는다.

OpenViking 0.4.16 wheel과 의존성 해석 결과:

- macOS Apple Silicon: OpenViking wheel의 플랫폼 태그는 macOS 14 이상이다. 실기동 검증 환경이다.
- macOS Intel: 제공된 OpenViking wheel의 태그는 macOS 15 이상이다. 실기동 미검증.
- Windows x86_64: Python 3.13, binary-only 의존성 해석을 확인했다. PowerShell에서 같은 uv 명령을 사용할 수 있지만 실기동 검증은 별도다.
- Linux x86_64/WSL2: Python 3.13, manylinux 2.35 기준 binary-only 해석을 확인했다. Ubuntu 22.04 이상을 기준으로 준비한다. glibc 2.28/2.31 기준의 전체 binary-only 해석은 실패했다.

Windows 네이티브 실행에서 네이티브 의존성 문제가 생기면 WSL2 Ubuntu 환경 안에 Day02를 두고 동일한 명령을 실행한다. Docker 설치는 필요하지 않다.
서버와 앱을 서로 다른 OS 환경에서 띄우기보다 같은 WSL 환경에서 실행하는 것이 경로·포트 관리를 단순하게 한다.

## 키와 설정

설정 우선순위는 프로세스 환경변수 → Day02/.env → 상위 .env다.
Day02/.env에 빈 키를 적으면 상위 파일의 키를 덮을 수 있으므로, 상위 키를 재사용할 때는 빈 API 키 항목을 만들지 않는다.

`server init`은 초기 설정을 생성하고, 기존 설정이 있으면 유지한다. 서버 설정은 초기화 시 모델 API 키를 저장하므로 키를 교체할 때는 서버를 중지하고 `.openviking/ov.conf`의 해당 API 설정을 갱신한 뒤 재시작한다.
서버 root 키와 client.json의 문서용 키는 모델 API 키와 다르다. 키가 담긴 파일은 공유하지 않는다.

강사 제공 서버를 선택적으로 연결하려면 두 항목을 함께 설정한다.

```dotenv
OPENVIKING_URL=http://서버주소:포트
OPENVIKING_API_KEY=문서용-계정-키
```

이 값이 있으면 RAG/적재는 해당 서버를 사용한다. `server` 명령은 계속 개인 로컬 서버만 제어한다.
검색의 고객/날짜 필터는 서버의 사용자 접근 권한을 대신하지 않는다.

## 자주 확인할 항목

### 포트 충돌

기본 포트는 19350이다. 초기화 전에 `.env`의 `OPENVIKING_PORT`를 다른 빈 포트로 정한다.
(예전 이름 `DAY02_PORT`도 계속 읽지만, 둘 다 있으면 `OPENVIKING_PORT`가 우선한다.)
이미 설정을 생성했다면 기존 서버를 stop한 뒤 ov.conf의 포트를 변경하고, 주소에 묶인 client.json의 설정도 확인한다.
다른 서버의 PID를 임의 종료하지 않는다.

### 401/403

1. 의도한 OPENVIKING_URL인지 확인한다.
2. 로컬 서버라면 `server start`의 문서 계정 초기화가 완료되었는지 확인한다.
3. shell 환경의 오래된 OPENVIKING_API_KEY가 파일 설정보다 우선하는지 확인한다.
4. root 키를 문서 API 호출에 사용하지 않는다.

### 원문·manifest 불일치

서버를 바꾸거나 원문을 수정했다면 `02_ingest.py` 또는 해당 catalog의 ingest를 다시 실행한다.
적재 완료 여부, 서버 URL, 원문 해시, 읽은 구간을 검사하므로 예전 manifest를 새 서버에 그대로 사용하지 않는다.
예전 색인은 자동 삭제하지 않는다.

### 모델/API 지연

문서 적재는 서버 내부의 요약·임베딩을 기다린다. status와 logs로 서버 상태를 확인한다.
질의 실패는 빈 검색 결과로 바꾸지 않고 오류로 반환하며 CLI는 exit code 1을 사용한다.
근거 부족 답변은 정상 실행 결과이며 `status=insufficient`다.

### HWP

```bash
uv run python scripts/install_rhwp.py
uv run day02 prepare data/samples/alpha-sla.hwp
```

`RHWP_BIN`을 지정하면 그 파일을 우선한다. 없으면 Day02/bin/rhwp(.exe), 그 다음 PATH에서 찾는다.
HWPX 기본 경로는 XML 본문/표 추출이며 물리 페이지 번호를 임의로 붙이지 않는다.

## 데이터 위치

`outputs/`에는 전처리 결과, 적재 manifest, 원문 검증 snapshot, 답변 trace, 비교 실험 결과가 있다.
이 파일들은 소스 배포의 필수 입력이 아니며 새 환경에서 다시 생성한다. `data/`와 `skills/`, 두 잠금 파일은 배포에 포함한다.

공식 참고: [OpenViking 서버](https://docs.openviking.ai/en/guides/03-deployment), [서버 설치 SOP](https://docs.openviking.ai/en/getting-started/04-setup-for-agent), [OpenViking CLI](https://docs.openviking.ai/en/getting-started/05-cli-setup), [DeepAgents Skills](https://docs.langchain.com/oss/python/deepagents/skills), [rhwp 0.8.6](https://github.com/edwardkim/rhwp/releases/tag/v0.8.6).
