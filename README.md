# League Rank Web

이 폴더 전체가 비공개 GitHub 저장소에 올릴 배포 패키지입니다. 방문자에게는 `public/`만 배포되며 TikTok 토큰, 프록시, 원본 수집 결과는 포함되지 않습니다.

## 1. GitHub 저장소

1. GitHub에서 새 저장소를 만들고 **Private**를 선택합니다.
2. 이 폴더 안의 파일과 숨김 폴더 `.github`까지 저장소 루트에 올립니다.
3. 저장소 `Settings > Secrets and variables > Actions`로 이동합니다.

다음 **Repository secrets**를 추가합니다.

- `TIKTOK_TT_TOKEN` (만료 시 새 토큰으로 교체, 또는 아래 `TIKTOK_SESSION_JSON` 사용)
- `TIKTOK_SESSION_JSON` (선택: `tiktok_auth.py --print-secret`으로 뽑은 세션 JSON)
- `TIKTOK_PERSONA_JSON` (선택: 실기기 `X-Argus`에서 추출한 기기 페르소나 JSON)
- `PROXY_1` ~ `PROXY_5` 중 사용하는 만큼
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_KV_NAMESPACE_ID`

프록시 예시: `https://user:password@host:port`

토큰이나 프록시는 `.env`, JavaScript, JSON, GitHub 코드에 직접 입력하지 마세요.

## 2. Cloudflare Pages

1. Cloudflare `Workers & Pages`에서 GitHub 저장소와 연결된 Pages 프로젝트를 만듭니다.
2. `Workers & Pages > KV`에서 `league-rank-data`라는 KV 네임스페이스를 만듭니다.
3. 생성된 KV 네임스페이스 ID를 GitHub Secret `CLOUDFLARE_KV_NAMESPACE_ID`에 넣습니다.
4. Cloudflare API Token에는 해당 계정의 `Workers KV Storage: Edit` 권한을 부여합니다.
5. Pages 프로젝트의 `Settings > Bindings`에서 Production KV 바인딩을 추가합니다.
   - 변수 이름: `RANKINGS_KV`
   - KV 네임스페이스: `league-rank-data`
6. 바인딩 적용을 위해 Pages를 한 번 다시 배포합니다.

사이트 주소는 `https://<프로젝트명>.pages.dev`입니다.

직접 갱신 버튼을 사용하려면 Pages 프로젝트 `Settings > Variables and Secrets`의 Production 환경에 다음 항목을 추가합니다.

**Secrets**

- `GITHUB_ACTIONS_TOKEN`: 해당 비공개 저장소 하나에만 접근하고 `Actions: Read and write` 권한만 가진 Fine-grained GitHub token

**Variables**

- `GITHUB_OWNER`: GitHub 사용자명 또는 조직명 (선택, 기본값 `chlye3232`)
- `GITHUB_REPO`: 저장소 이름 (선택, 기본값 `league-rank`)
- `GITHUB_BRANCH`: `main`
- `GITHUB_WORKFLOW_FILE`: `refresh-and-deploy.yml`

GitHub 토큰은 브라우저로 전달되지 않고 Pages Function에서만 사용됩니다. 직접 갱신 버튼은 별도의 비밀번호 입력 없이 실행되며, 한 브라우저·IP에서는 10분에 한 번만 요청할 수 있고 이미 실행 중인 갱신이 있으면 중복 요청이 거부됩니다.

## 3. 갱신과 KV 저장

`.github/workflows/refresh-and-deploy.yml`이 그룹별 주기에 따라 다음 작업을 수행합니다.

1. 저장된 스냅샷, 세션 캐시(`output/tiktok-session.json`) 및 ID 캐시 복원
2. 필요 시 `token_beat`로 세션 유지 및 응답 `x-tt-token`/쿠키/TicketGuard 자동 저장
3. 프록시 없이 해당 그룹을 먼저 갱신
4. 인증 만료(`먼저 로그인하세요`) 시 불필요한 재시도를 중단하고 실패 기록, 일시 차단 시 실패한 리그만 구매 프록시로 즉시 재시도
5. 그래도 실패하면 5분 기다린 후 해당 리그만 마지막 재시도
6. `rank`, `id`만 포함한 공개 JSON 생성
7. 공개 JSON을 Cloudflare KV의 `rankings` 키에 저장

Pages는 코드가 바뀔 때만 배포합니다. 예약 갱신과 직접 갱신은 Pages를 다시 배포하지 않고 KV 데이터만 교체합니다.

자동 갱신 주기는 다음과 같습니다.

- A1~A3: 2시간
- B1~B5: 1시간 30분
- C1~C5: 1시간
- D1~D5: 30분

각 그룹은 서로 다른 분에 시작하도록 분산되어 있습니다. 한 그룹 안에서도 후보 ID를 동시에 요청하지 않고 최대 50명까지 순서대로 검사합니다.

갱신 이력, 실패 목록과 실행 로그는 GitHub Actions의 실행 요약 및 비공개 아티팩트에 90일간 보관됩니다. 마지막 재시도 후에도 실패하면 기존 저장 데이터를 유지하고 해당 그룹의 다음 정기 갱신에서 다시 시도합니다. 이 기록들은 `public/`에 포함되지 않습니다.

수동 갱신은 웹의 `직접 갱신` 버튼 또는 GitHub 저장소 `Actions > Refresh and deploy league rankings > Run workflow`에서 실행합니다. 웹에서는 처음에 아무 리그도 선택되지 않으며, 원하는 리그만 고르거나 `전체 선택`·`전체 해제`를 사용할 수 있습니다. 선택한 리그만 순차적으로 갱신됩니다. GitHub에서 직접 실행할 때도 `leagues`에 `A1,B3,D5`처럼 쉼표로 구분한 리그를 입력하면 해당 리그만 갱신합니다.

## 4. 로컬 확인

```powershell
python build_static_site.py
python -m http.server 8788 --directory public
```

브라우저에서 `http://127.0.0.1:8788`을 엽니다.

## 공개되는 파일

- `public/index.html`
- `public/styles.css`
- `public/app.js`
- `public/data/rankings.json`
- `public/_headers`
- `public/_routes.json`

`public/data/rankings.json`에는 리그, 순위와 TikTok 아이디만 들어가며 KV가 비어 있거나 연결되지 않았을 때 사용하는 예비 데이터입니다. 정상 운영 중에는 웹이 `/api/rankings`를 통해 KV의 최신 데이터를 읽습니다. 갱신 시각, 실패 기록과 실행 로그는 공개되지 않습니다.

`public/_worker.js`는 KV 조회와 직접 갱신 버튼의 서버 측 API입니다. GitHub 토큰은 응답이나 정적 파일에 포함되지 않습니다.
