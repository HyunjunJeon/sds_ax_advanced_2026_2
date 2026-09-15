# dcode 분석 작업공간 안내 (로컬 전용 · git 제외)

이 저장소는 dcode 소스 분석 작업공간의 일부다. **작업을 시작하기 전에 전체 가이드를 읽는다:**
`/Users/jhj/Desktop/sds_ax_advanced_2026_2/dcode-analysis/AGENTS.md`

(이 파일이 짧은 안내만 담는 이유: dcode는 프로젝트 루트 밖을 가리키는 심볼릭 링크를 보안상 불러오지 않으므로, 상위 가이드를 링크로 연결할 수 없다.)

## 핵심 규칙 요약

- 답변과 새 문서는 한국어로 쓴다.
- **기능 파악 순서**: 가이드 §3 기능 색인 → `dcode-analysis/analysis/NN-*.md` → GitNexus로 호출 관계 확인 → 소스의 `# [해설]` 주석과 함께 읽기 → 테스트로 의도 확인.
- **해설 주석**: `libs/code/deepagents_code/`의 핵심 35개 파일에 `# [해설]` 주석이 있다. `[흐름]`·`[설계]`·`[SDK]`·`[문서 불일치]`·`[주의]` 태그로 grep한다.
- **GitNexus**: 레지스트리 이름 `deepagents-dcode`. MCP 도구(`query`, `context`, `impact`)에 `repo="deepagents-dcode"`를 넣는다. dcode는 함수 내부 import를 많이 써서 호출자가 빠질 수 있으므로, "호출자 없음/적음" 결과는 `grep -rn '<심볼>' libs/code/deepagents_code`로 교차 확인한다.
- **줄 번호**: 작업 트리(브랜치 `annotated-ko`)는 주석 때문에 줄이 밀려 있다. `analysis/`의 `path:line`은 태그 `baseline-1d3232c` 기준이므로 `git show baseline-1d3232c:<path>`로 보거나 심볼명으로 찾는다.
- **수정 금지**: 코드·docstring을 바꾸지 않는다. 허용되는 변경은 `# [해설]` 주석 추가뿐이며, 추가 후 `dcode-analysis/`에서 `python3 scripts/check_comments_only.py --changed`로 검증한다.
- dcode 실행·원격 샌드박스 검증(과금 가능)은 사용자 요청이 있을 때만 한다.
