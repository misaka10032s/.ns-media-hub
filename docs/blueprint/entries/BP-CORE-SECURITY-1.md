---
id: BP-CORE-SECURITY-1
title: 同源 CSRF 防護 + SSRF 防護（misc.py 共用 guard）
system: backend-core
tags: [backend, security, csrf, ssrf]
status: 已完成
request_verbatim: "@PM registry ns-media-hub.md Roadmap（現行未勾待辦）：「Cookie 變更 API — 無 auth/CSRF 保護（資安：任意請求皆可改 cookie）」"
decided_date: 2026-06-20
exec_links:
  - app/api/routes/misc.py
  - app/api/routes/downloaders.py
  - app/api/origin_guard.py
  - app/api/app.py
  - app/config/settings.py
  - app/domain/network_safety.py
  - app/providers/direct_file/provider.py
  - app/services/discord_service.py
  - module/server.py
qa_log:
  - date: 2026-09-07
    q: "@PM 待回答 #47（本機 Web UI 寫入端點缺 Origin/Host 驗證——同源假設下，任意頁面或 DNS-rebinding 皆可對 127.0.0.1:7601 發 POST/PUT/PATCH/DELETE；現行只有 cookies 與 downloaders/update 兩個端點有 `_check_same_origin`，其餘全無防護）＋ #48（`direct_file` provider 與 Discord embed 下載路徑完全繞過既有 `_is_safe_url`，構成 SSRF：可誘使伺服器對內網／保留位址發出請求）——是否要修、怎麼修？"
    a: "站主裁定（2026-09-07，逐字）：「五個都做」；「#48 併進 #47 同一批修：精確比對 host＋解析 IP 擋內網／保留位址＋轉址後每跳重驗，重用既有 `_is_safe_url`」。已按此實作：\n\n1. 新增全域 `app/api/origin_guard.py`（`app.before_request`，涵蓋所有現在與未來的 POST/PUT/PATCH/DELETE 路由，不需逐路由加）：`Host`（若存在）須為 loopback 且 port 精確等於 `app.config.settings.API_PORT`（缺 port 一律視為 scheme 預設 80，絕不跳過比對）；`Origin`（若存在）須精確比對允許清單，`null` 一律拒絕；兩者都缺（本機 CLI 呼叫，如 curl）僅靠 Host 檢查放行；GET/HEAD/OPTIONS 豁免。與既有 `_check_same_origin`（`app/api/routes/misc.py`，套用於 cookies、downloaders/update）並存、不取代——兩者同時檢查只會更嚴，從不放寬。\n2. `API_PORT` 併入 `app/config/settings.py` 單一來源（`NS_MEDIA_HUB_PORT`，預設 7601），guard 與 `run_server()` 共讀同一值。允許清單另可用 `NS_MEDIA_HUB_EXTRA_ORIGINS`（逗號分隔）加白名單，含 `*` 的項目一律丟棄並印 WARNING。\n3. `_is_safe_url` 移到共用模組 `app/domain/network_safety.py`（`app.domain` 在 import-linter 分層低於 `app.api`/`app.services`/`app.providers`，三邊皆可合法引用）：改為檢查『每一個』`getaddrinfo` 解析出的位址（原本只查第一個）、拒絕內嵌帳密（`user:pass@host`）、新增 `is_multicast`／`not is_global` 判斷（涵蓋 100.64.0.0/10 這類 `is_private` 判不到的殘留私有段）。`misc.py` 的 `_is_safe_url` 改為對它的別名匯入，行為不變。\n4. 套用到先前完全沒有防護的兩處：`app/providers/direct_file/provider.py::download()`（原本裸用 `urlopen()`，預設自動跟隨轉址——一個一開始通過檢查的 URL 可以 302 到內網後才真正發出請求）與 `app/services/discord_service.py::_download_embed_image()`（Discord 訊息 embed 圖片 URL 是攻擊者可影響的內容，原本裸用 aiohttp 預設 `allow_redirects=True`，也完全沒檢查）。兩者都改為手動跟隨轉址：每一跳（含第一個 URL 本身）先過 `is_safe_url()` 才發出請求，最多 `MAX_REDIRECT_HOPS=5` 跳，超過視為不安全。\n5. **已知殘留（依站主裁定原文本身即預期的例外）**：gallery-dl／yt-dlp 走外部 CLI 子行程（`app/providers/gallery_dl/provider.py`、`app/providers/ytdlp/provider.py`），其內部轉址完全不可控，本批**未**加初始 URL 的 `is_safe_url()` 前置檢查——原因：這兩個 provider 的既有測試（`test_gallery_dl_auth_retry.py`、`test_gallery_dl_error_capture.py`、`test_ytdlp_auth_cooldown.py` 等）用真實可解析的公開網域（danbooru.donmai.us、x.com、youtube.com…）驅動 `download()`，加一道會做真實 DNS 解析的前置檢查會讓這批既有單元測試在離線/CI 環境下依賴真實網路，且需要在多個測試檔案 mock `socket.getaddrinfo`——影響面遠超過本次「最小變更」範圍，故未做，留待日後若要補強再獨立處理。"
  - date: 2026-09-07
    q: "同批次的 opus 新鮮複核（fresh reviewer，未寫過這段程式碼）對 commit a148bc0 的判定：VERDICT CHANGES-NEEDED，擋在合併前的三個發現——F1（本專案自己的 chromeExtension／ 對 /api/jobs 發 POST 時帶 `Origin: chrome-extension://<id>`，不在允許清單內，直接 403；`NS_MEDIA_HUB_EXTRA_ORIGINS` 當時也無法表達這個 scheme，站主完全沒有繞過方法，屬功能性 regression）、F2（`is_safe_url` 對超長主機名稱丟出 `UnicodeError`，未被 `except socket.gaierror` 接住，`POST /api/fetch_status` 500 而非設計中的 400；docstring「never raises」不實）、F3（`module/server.py` 仍寫死 port 7601，`NS_MEDIA_HUB_PORT` 改了會讓這個進入點與 guard 要求的 port 對不上，導致其上所有寫入請求 403；`settings.py` 的合併說明註解對 `LOCAL_API_BASE` 描述不實）。另外要求補記 F6（Vite 允許清單措辭失準，`[::1]` 未涵蓋 5173）、DNS 二次解析殘留、F9（gallery-dl／yt-dlp 殘留的成本說法站不住腳）到本 blueprint。"
    a: "已在同一 commit 後的修正批次全部處理：origin_guard.py 的 `_parse_extra_origins` 新增接受精確 `chrome-extension://<32碼 a-p 小寫>` 項目（不放行整個 scheme、不放行 `*`），並在每個處理程序內對每個被拒絕的 Origin 值只印一次 WARNING（附可直接貼上的 `NS_MEDIA_HUB_EXTRA_ORIGINS=` 設定行）；`network_safety.py::is_safe_url` 的 `except` 子句擴大為 `(socket.gaierror, UnicodeError, OSError)`，docstring 改為誠實描述「這些情形都回傳 (False, reason) 而非丟例外」；`module/server.py` 改讀 `app.config.settings.API_PORT`，`settings.py` 的 `LOCAL_API_BASE` 改由 `API_PORT` 推導、註解改為如實描述目前真正合併到單一來源的欄位。F6／DNS-TOCTOU／F9 三段記錄已補進上方設計說明章節。三項行為變更均補了對應測試，見下方 `tests`。"
  - date: 2026-09-23
    q: "站主 09-23 回報擴充功能右鍵「下載此內容」對 facebook reel 被拒：「拒絕請求，因為 Origin 不在允許清單內：'chrome-extension://ilnjgmakcodkkgomcfbeliencopgcmal'」，問「為什麼突然被擋? 之前都沒問題 我也沒改啥」。"
    a: "原因：09-07 的 Origin 檢查（cd405a7）上線後 hub 一直沒重啟，09-23 08:40 站主重開 dl.cmd -sb 才生效，而 .env 從未加 NS_MEDIA_HUB_EXTRA_ORIGINS（待回答 #47＋48 一直未做）。站主裁定「那你就給我去加 不要給我搞個功能又不處理」→ PM 代加 `NS_MEDIA_HUB_EXTRA_ORIGINS=chrome-extension://ilnjgmakcodkkgomcfbeliencopgcmal`（.env 備份 .env.bak-20260923）並以隱藏視窗重啟 hub，兩筆 POST /api/jobs 回 202。擴充功能 ID 由 manifest 無 key 的載入路徑決定，同一資料夾載入即穩定。同日下載本身另因 facebook cookie 檔讓 yt-dlp `Cannot parse data` 失敗，修法見 main 9ad6c0f（帶 cookie 失敗改無 cookie 重試一次）。"
origin: "`_check_same_origin()`/`_is_safe_url()` 首次入庫於 commit f2718f1（2026-06-20，「feat+security(ns-media-hub): docs sync, /gallery SPA fallback, cookie CSRF guard, tests, WAL」）；本次 Origin/Host guard + SSRF 強化於 2026-09-07 分支 `fix/origin-guard-ssrf`（commit a148bc0），opus 複核 F1-F3 修正於同分支後續 commit"
tests:
  - date: 2026-09-07
    target: "py -3.11 quality-gates/run.py l1（G1 ruff、G2 mypy、G3 pytest、G4 import-linter、G5 diff-cover）"
    action: "worktree .claude/worktree/origin-guard-ssrf 內，實作 #47/#48 後跑完整 l1 gate"
    expected: "0 個相對 baseline 的新 finding；全套測試綠燈；diff coverage >= 60%"
    result: "PASS — [G1] 48 total ruff violation(s), 0 new vs baseline；[G2] 12 total mypy error(s), 0 new vs baseline；484 passed, 1 warning（439 既有 + 45 新增：test_origin_guard.py 18、test_network_safety.py 16、test_direct_file_ssrf.py 6、test_discord_embed_ssrf.py 5）；[G4] 4 total import violation(s), 0 new vs baseline；[G5] diff coverage 92% (Total: 155 lines, Missing: 11 lines) >= 60% threshold"
  - date: 2026-09-07
    target: "py -3.11 -m pytest -q ＋ py -3.11 quality-gates/run.py l1 ＋ py -3.11 D:/backup/CSIA/@PM/.claude/tools/blueprint-build.py --lint（同一 worktree，F1-F3 修正後）"
    action: "同 worktree 內，補齊 opus 複核 F1（chrome-extension 具體 id 允許清單＋一次性 WARNING）、F2（`is_safe_url` 例外面擴大＋docstring 更正）、F3（`module/server.py`／`LOCAL_API_BASE` 改讀 `API_PORT`）三項行為變更，各補測試後重跑全套 l1 gate、pytest 全量、blueprint lint"
    expected: "0 個相對 baseline 的新 finding；全套測試綠燈；diff coverage >= 60%；blueprint lint 無新 warning"
    result: "PASS — [G1] 48 total ruff violation(s), 0 new vs baseline (48 pre-existing)；[G2] 12 total mypy error(s), 0 new vs baseline (12 pre-existing)；[G3b] PASS — 6 changed test file(s), all touched test functions assert something；[G4] 4 total import violation(s), 0 new vs baseline (4 pre-existing)；[G5] PASS — diff coverage >= 60%（詳細：Total 172 lines, Missing 11 lines, Coverage 93%）；`py -3.11 -m pytest -q` → 498 passed, 1 warning（484 既有 + 14 新增：test_origin_guard.py +12、test_network_safety.py +2）；blueprint lint → warning: BP-SVC-DOUJIN-1.md body has 274 lines (>200)；warning: BP-SVC-DOUJIN-IMPORT-1.md body has 202 lines (>200)；lint OK (2 warning(s))（兩則皆為既有、與本次無關）"
---

## 設計說明

`app/api/routes/misc.py` 提供兩個跨端點共用的安全 guard：

### 同源 CSRF 防護（`_check_same_origin`）

檢查請求的 `Origin`（優先）或 `Referer` header 是否為 `127.0.0.1`/`localhost`；
兩者皆缺（如 `curl` 直接呼叫）視為本機工具呼叫允許通過；host 不符則回
`(False, "拒絕跨來源的 cookie 變更請求。")`，呼叫端回 HTTP 403。目前套用於：

- `POST`/`PUT`/`DELETE /api/cookies`（`BP-SVC-CREDENTIALS-1`）
- `POST /api/downloaders/update`（`BP-SVC-UPDATER-1`，`app/api/routes/downloaders.py`
  直接 import 復用同一函式）

### SSRF 防護（`_is_safe_url`）

`POST /api/fetch_status`（`app.py` 內建的除錯用「探測 URL 是否可存取」端點）呼叫，
拒絕非 http/https scheme，並用 `socket.getaddrinfo` 解析主機後檢查
`is_loopback`/`is_private`/`is_link_local`/`is_reserved`/`is_unspecified`，
防止此端點被用來探測內網（SSRF）。

### 明確已知的殘留缺口（程式碼自己的 docstring 標註）

`_check_same_origin` 的 docstring 明確自陳：「這是 same-origin CSRF check only ——
this app's mutating endpoints 完全沒有 session/token 驗證……只在嚴格本機
（127.0.0.1）情境下算安全，若此伺服器曾對外暴露則不安全」。這不是本次盤點發現的
新問題，是原始碼作者自己已記錄的設計取捨，本條目原樣轉載以維持誠實。

### 誠實現況 —— 本次盤點的重要修正

**@PM registry 現行仍記載「Cookie 變更 API — 無 auth/CSRF 保護」為未勾選待辦**，
但這個防護實際上已於 2026-06-20（commit f2718f1）加入，`tests/test_csrf_protection.py`
6 個案例覆蓋「無 header／本機 Origin／本機 named Origin／外部 Origin／外部
Referer／本機 Referer」情境。Registry 對此項目的記載已過期，本 blueprint 條目
以程式碼與 git 歷史為準；建議下次 registry 維護時同步勾除。

---

## 2026-09-07 補強 —— 待回答 #47（全域 Origin/Host guard）+ #48（direct_file / Discord embed 的 SSRF）

上面兩節記錄的是 2026-06-20 的原始防護，**只涵蓋 cookies 與
downloaders/update 兩個端點**。這次盤點（待回答 #47）發現其餘所有 mutating 路由
（`/api/jobs`、`/download`、`/api/jobs/<id>/retry`、`/api/history`
GET 以外的方法、`/api/history/requeue`、`/api/gallery/doujin/*` 的 PUT/POST/DELETE）
完全沒有同源檢查；同時待回答 #48 發現 `_is_safe_url` 只套用在除錯用的
`/api/fetch_status`，`direct_file` provider 與 Discord embed 下載這兩個「會真的把
使用者/攻擊者提供的 URL 拿去發請求」的路徑完全沒套用。詳細裁定與變更內容見上方
`qa_log`。以下記錄新增的機制本身：

### 全域 Origin/Host guard（`app/api/origin_guard.py`）

以 `app.before_request` 註冊，對**所有**現在與未來的 POST/PUT/PATCH/DELETE
路由生效（不需逐路由 opt-in）——與 `_check_same_origin` 並存，不取代：後者
在它原本套用的兩個端點上依然生效，兩層防護同時檢查只會更嚴。

- `Host`（若存在）：須為 loopback（127.0.0.1／localhost／::1）且 port **精確**
  等於 `app.config.settings.API_PORT`（單一設定來源，`NS_MEDIA_HUB_PORT` 環境變數，
  預設 7601）——缺 port 一律視為 HTTP scheme 預設值 80 去比對，不會因為缺 port
  就跳過檢查。
- `Origin`（若存在）：須**精確**比對 `resolve_allowed_origins()`（loopback ×
  `API_PORT`（含 `[::1]`）、127.0.0.1／localhost × 5173（Vite dev port，**不含**
  `[::1]`）、`NS_MEDIA_HUB_EXTRA_ORIGINS` 環境變數加的白名單——含 `*` 的項目
  一律丟棄並印 WARNING，不會被當成萬用字元接受或整條規則失效）。`null` 一律
  拒絕。
- 兩者皆缺（本機 CLI 呼叫如 curl）僅靠 Host 檢查放行——瀏覽器在跨來源／
  同源的 state-changing 請求上一定會帶 `Origin`，所以「沒有 Origin」不是遠端
  攻擊者可利用的繞過路徑。
- GET/HEAD/OPTIONS 豁免。
- **Chrome 擴充功能的 Origin（2026-09-07 補強，見下方 qa_log）**：本專案自己的
  `chromeExtension/` 對 `/api/jobs` 發 POST 時，瀏覽器會附上
  `Origin: chrome-extension://<擴充功能 id>`，預設不在允許清單內（故意——整條
  `chrome-extension://` scheme 全放行等於放行使用者瀏覽器裡裝的「每一個」擴充
  功能，而非只放行這一個）。`NS_MEDIA_HUB_EXTRA_ORIGINS` 現在額外接受**一個
  具體 id** 的 `chrome-extension://<32碼 a-p 小寫>` 項目（同樣精確比對、不含
  路徑、不含 `*`）；被拒絕的 Origin 值每個處理程序只印一次 WARNING，內容直接
  給出可貼上的 `NS_MEDIA_HUB_EXTRA_ORIGINS=<該值>` 設定行。

### SSRF 驗證器移到共用模組（`app/domain/network_safety.py`）

`_is_safe_url` 的實作搬到 `app.domain.network_safety.is_safe_url`（`app.domain`
在 `pyproject.toml` `[tool.importlinter]` 分層裡低於 `app.api`／`app.services`／
`app.providers`，三邊都能合法引用，不會新增跨層違規）。`misc.py` 保留
`_is_safe_url` 這個名字，但改成對新模組的別名匯入，行為與呼叫端不變。相對原本
的實作，強化了：

- 檢查 `getaddrinfo` 解析出的**每一個**位址（原本只查 `[0]`，多筆 A/AAAA
  紀錄時後面的位址完全沒被檢查）。
- 拒絕內嵌帳密的 URL（`user:pass@host`）。
- 新增 `is_multicast`／`not ip.is_global` 判斷，涵蓋 `is_private` 判不到的殘留
  私有段（例如 100.64.0.0/10 共享位址空間，雲端 metadata endpoint 常見的鄰近段）。
- IPv4-mapped IPv6（`::ffff:127.0.0.1`）攤平後再判斷。

### 套用到先前完全沒防護的兩個真實 fetch 路徑

- **`app/providers/direct_file/provider.py::download()`**：原本裸用
  `urlopen()`，預設自動跟隨轉址——一個一開始通過 `is_safe_url()` 檢查的 URL
  可以合法地 302 到內網位址，檢查形同虛設。改為 `_NoRedirectHandler` 停用自動
  跟隨，手動迴圈跟隨最多 `MAX_REDIRECT_HOPS`（5）跳，**每一跳（含第一個 URL
  本身）都先過 `is_safe_url()` 才發出請求**。
- **`app/services/discord_service.py::_download_embed_image()`**：Discord
  訊息 embed 的圖片 URL 是頻道內任何人可影響的內容（不像 attachment 走 Discord
  自己的 CDN），原本裸用 aiohttp 預設 `allow_redirects=True`，同樣完全沒檢查。
  改為 `allow_redirects=False` + 手動跟隨迴圈，邏輯與 direct_file 對稱。
  `_save_attachment`（Discord CDN attachment，非任意網址）未變動。

### 已知殘留（本次刻意不做，原因見上方 `qa_log`）

`app/providers/gallery_dl/provider.py`、`app/providers/ytdlp/provider.py`
的下載動作是外部 CLI 子行程（`gallery-dl`／`yt-dlp` 本身發出的請求），本次未加
初始 URL 的 `is_safe_url()` 前置檢查。**成本更正（2026-09-07 opus 複核 F9）**：
只有三個測試檔驅動這兩個 provider 的 `download()`（`test_gallery_dl_auth_retry.py`、
`test_gallery_dl_error_capture.py`、`test_ytdlp_auth_cooldown.py`，共 22 處
`download(` 呼叫），且 `tests/conftest.py` 已有一個 repo 全域的 `autouse=True`
fixture，其設計目的正是「用一處 `monkeypatch` 結構性地擋掉整類問題，而非逐一
改測試」——比照該 fixture 的做法，補一行
`monkeypatch.setattr(socket, 'getaddrinfo', …)` 即可一次涵蓋全部 22 處呼叫，
約十行程式碼，**不是**「影響面遠超過本次最小變更範圍」的大改動。維持不做的
理由改為：這是獨立、可另外派工的小任務，且即使做了也只能擋「直接」指向內網
的 URL——`gallery-dl`／`yt-dlp` 自己在子行程內跟隨的轉址仍完全不可控，防護
本質上只是部分的。若站主要補，走獨立 dispatch 即可。

### DNS 二次解析（TOCTOU）殘留（2026-09-07 opus 複核，未修）

`is_safe_url()` 用 `socket.getaddrinfo` 解析一次、判斷位址安全後就回傳；實際
發出請求時，`urllib`（`direct_file` provider）／`aiohttp`（Discord embed）會
在連線階段**再解析一次**同一個主機名稱。兩次解析之間若目標主機的 DNS 被攻擊者
控制並「換答案」（DNS rebinding），第二次解析可以回傳與第一次不同、指向內網
的位址，繞過已經做過的檢查——這是檢查與實際使用之間的時間差（Time-Of-Check to
Time-Of-Use）問題，不是 `is_safe_url()` 本身的邏輯錯誤。**沒有低成本修法**：
真正的修法要換一個自訂的傳輸層，讓連線階段直接撥打「檢查時解析到的那個 IP」，
同時仍保留 TLS SNI 與 HTTP `Host` header 指向原本的網域名稱（否則 TLS 憑證驗證
與虛擬主機路由都會壞掉）——這需要客製 `HTTPConnection`／`aiohttp.TCPConnector`
層級的改動，非本次批次範圍。**前提條件**：攻擊者必須能控制目標主機的 DNS 回應
（例如攻擊者自己架設、可切換答案的網域），威脅面比「直接餵一個內網 URL」窄
很多，但並非零。本次刻意不修，留下這段記錄以保持誠實。
