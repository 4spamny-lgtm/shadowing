# 쉐도잉 — PC 없이 유튜브 자막 추가하기

기존 HTML·GitHub Pages·영상 JSON 포맷은 그대로 사용합니다. HTML 수정은 필요 없습니다.

## 설치 (2개 파일 교체 + API 키 등록)

1. GitHub 저장소 `4spamny-lgtm/shadowing`에서 기존 `prep.py`를 백업한 후, 이 패키지의 `prep.py`로 교체합니다.
2. 저장소 `.github/workflows/add_video.yml`을 동봉한 파일로 교체합니다. **두 파일은 함께 교체**하세요.
3. [Supadata](https://supadata.ai/)에서 발급받은 API 키를 GitHub 저장소 **Settings → Secrets and variables → Actions → New repository secret**에 추가합니다.
   - Name: `SUPADATA_API_KEY`
   - Secret: 발급받은 키
4. GitHub Actions → `영상 추가` → `Run workflow`로 테스트합니다. 기존 웹앱의 `+ 영상 추가`에 유튜브 주소를 입력해도 자동 실행됩니다.
5. 등록이 완료되면 웹앱에서 `↻`를 눌러 목록을 확인합니다. GitHub Pages 반영에는 시간이 걸릴 수 있습니다.

## 무엇이 달라졌나요?

- 원래의 `yt-dlp` 및 JSON 형식, 문장 분리, GitHub 저장 구조를 유지합니다.
- `SUPADATA_API_KEY`가 있으면 **GitHub Actions에서 자막 API를 우선 사용**하여 GitHub 서버의 YouTube 403 경로를 피합니다.
- 자막 API의 `offset`/`duration`(밀리초)을 기존 `words`/`sentences`(초)로 변환합니다.
- 비동기 작업(202 응답)도 처리합니다.
- 등록 실패한 주소는 기존과 같이 `queue.txt`에 남으며, Actions에 경고가 표시됩니다.
- `add.bat`는 삭제하지 않고 백업 수단으로 계속 사용할 수 있습니다.

## 주의 사항

- **외부 서비스 등록과 API 키가 필요합니다.** 무료 제공량과 과금 정책은 가입 시 최신 안내를 확인하세요. AI 전사에 따른 추가 비용을 방지하려고 `mode=native`(기존 자막만)로 요청합니다.
- 공개 자막이 없는 영상이나 외부 서비스가 지원하지 않는 영상은 계속 대기 목록에 남습니다.
- API에서 제공하는 타임스탬프는 구간 단위입니다. 기존 yt-dlp의 단어별 시각과 달리 **단어별 시각은 글자 길이 비율로 추정**하므로 문장 경계/싱크 정밀도가 떨어질 수 있습니다. 기존 앱에서 편집 가능합니다.
- 기존 Python `prep.py`는 그대로 유지되지 않고 **수정된 버전으로 교체**됩니다. PC에서 `add.bat` 실행 시에도 수정본이 동작하며, GitHub Actions API 키는 PC에 자동 전달되지 않습니다.
- HTML에 API 키를 넣지 마세요. 공개 GitHub 파일이나 브라우저 localStorage에 보관하지 않는 구조입니다.
- 이 패키지는 API 키가 없어 실제 Supadata API 호출까지는 검증하지 못했습니다. 패키지의 로컬 단위 테스트는 통과했습니다.

## 다시 원래대로 돌아가려면

GitHub에서 이전 커밋의 `prep.py`, `.github/workflows/add_video.yml`을 복원하면 됩니다. 이미 저장된 `videos/*.json`에는 영향을 주지 않습니다.

## 공식 API 문서

- https://github.com/supadata-ai/skills/blob/main/skills/supadata/references/video.md
