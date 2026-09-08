---
id: BP-SVC-GALLERY-1
title: 媒體庫瀏覽服務（gallery_service + /api/gallery/* 含 Range 串流）
system: backend-service
tags: [backend, service, gallery, media, api]
status: 已完成
request_verbatim: "@PM registry ns-media-hub.md Roadmap（已勾）：「Media viewer — GalleryView.vue + /api/gallery/* 完整實作（佐證：commit ff31884 親測確認）」"
decided_date: 2026-05-25
exec_links:
  - app/services/gallery_service.py
  - app/api/routes/gallery.py
  - tests/test_gallery_service.py
qa_log:
  - date: 2026-09-08
    q: "@PM 待回答 #53 item 3：一般模式來源的 `list_categories()` 每次請求都對整棵目錄樹做完整遞迴（`_iter_leaf_items`）才能算出 `item_count` 與「這個分類是不是空的」——真實 `DOWNLOAD_DIR` 有約 182,000 個檔案／約 3,000 個目錄／126 個頂層分類，一個有 30 個子資料夾、每個子資料夾 500 張圖的來源，`item_count` 會顯示 30×500=15000 而不是 30。要不要改，怎麼改？"
    a: "站主裁定 2026-09-07（逐字）：「非空找到第一個就停；一般模式數字＝直接子項目數，不快取不分頁」——三件事都做，DB thread-local connection 那個方向（PM 分析裡提過）不在這次裁定範圍內，沒有做。實作：`gallery_service.py` 新增 `_has_any_child()`（單次 `os.scandir`，找到第一個非隱藏直接項目就 `return True`，從不遞迴進子目錄）與 `_count_immediate_children()`（單次 `os.scandir`，只數直接子項目，files+dirs 都算 1 個，不遞迴）；`list_categories()` 對一般模式來源改呼叫這兩個函式，不再呼叫 `_iter_leaf_items()`。`_iter_leaf_items()` 本身不變、不刪除——`list_items()`（實際打開一個分類看內容時）仍然需要真正的葉節點遞迴，這次沒有動它，也沒有加快取或分頁（裁定明確排除）。doujinshi 模式（`_count_immediate_subdirs`）完全沒有動——它已經是同款「只數直接子項目」邏輯。前端 `GalleryView.vue` 的徽章只是一個裸數字（`{{ cat.item_count }}`），沒有任何文字暗示「總圖片數」，不需要改文案。量測（同一支解譯器行程內背靠背跑，見下方 `tests`）：合成 126×24×60 檔案樹（181,440 個檔案，比例對應真實 DOWNLOAD_DIR 的 126 個頂層分類）——舊演算法（`_iter_leaf_items` 遞迴等價實作）31.17 秒，新演算法（`_has_any_child`+`_count_immediate_children`）0.0443 秒，快 703.8 倍，兩者算出的分類數與 sample item_count 一致（126 個分類、`item_count=24`，即直接子項目數）。"
origin: "`app/services/gallery_service.py` 首次入庫於 commit ff31884（2026-05-25）；一般模式 `item_count`／非空判斷改為直接子項目計數於 2026-09-08 分支 `fix/q53-gallery-count`（@PM 待回答 #53 item 3 裁定）"
tests:
  - date: 2026-09-08
    target: "py -3.11 quality-gates/run.py l1（G1 ruff、G2 mypy、G3 pytest、G4 import-linter、G5 diff-cover）"
    action: "worktree .claude/worktree/q53-gallery-count 內，實作 #53-3 後跑完整 l1 gate"
    expected: "0 個相對 baseline 的新 finding；全套測試綠燈；diff coverage >= 60%"
    result: "PASS — [G1] 47 total ruff violation(s), 0 new vs baseline（47 pre-existing，含一筆因新測試真的用到 `patch` 而消失的既有 baseline 項目，已確認是真修正並 `--update-baseline` 重新快照）；[G2] 12 total mypy error(s), 0 new vs baseline；500 passed, 1 warning（498 既有 + 2 新增：test_general_category_item_count_is_direct_children_count、test_general_non_empty_check_stops_at_first_item_no_full_walk）；[G3b] PASS — 1 changed test file(s), all touched test functions assert something；[G4] 4 total import violation(s), 0 new vs baseline；[G5] PASS — diff coverage >= 60%"
  - date: 2026-09-08
    target: "一次性效能量測腳本（scratchpad，非 repo 內測試）：合成 126×24×60 檔案樹（181,440 檔案），同一解譯器行程內背靠背跑舊/新演算法"
    action: "OLD＝`_iter_leaf_items()` 遞迴演算法的逐字重現版本；NEW＝真正的 `gallery_service.list_categories()`（`DOWNLOAD_DIR` 指向合成樹），量測 wall-clock"
    expected: "NEW 遠快於 OLD，且兩者算出的分類數／item_count 一致（證明只是換算法，不是換答案）"
    result: "PASS — OLD 31.1700s／126 categories／sample item_count=24；NEW 0.0443s／126 categories／sample item_count=24；速度提升 703.8 倍"
---

## 設計說明

`app/services/gallery_service.py`把 `download/` 目錄樹轉成前端媒體庫可用的
分類/項目/檔案三層結構，供 `BP-VIEW-GALLERY-1` 消費。

### 三層瀏覽模型

1. **分類（category）**：`download/` 下的一級子目錄（如 `pixiv.net`、`discord`）。
2. **項目（item）**：分類內的「葉節點」——`_folder_has_only_files()` 判斷一個目錄
   是否「只含檔案、不含子目錄」，是則視為一個 item（如某作者的整個相簿）；不是則
   視為中繼目錄（如 `pixiv.net/`）並遞迴往下找，直到找到真正的葉節點。單一散落檔案
   （不在任何目錄內）也視為獨立 item。這條路徑（`_iter_leaf_items`）現在只有
   `list_items()`（實際打開一個分類看內容）在用——見下方「一般模式 item_count 改算法」。
3. **檔案（file）**：item 目錄內的實際檔案清單。

### 分類清單（`list_categories()`）的 item_count 與非空判斷

兩種模式各自一套定義，互不影響：

- **doujinshi 模式**（wnacg／nhentai／18comic）：`_count_immediate_subdirs()`——
  一個子資料夾＝一本書，`os.scandir` 單次掃描，不遞迴。這是本子模式本來就有的
  設計（見 `BP-SVC-DOUJIN-1`），這次沒有動。
- **一般模式**（pixiv、discord、ytdlp…其餘全部）：2026-09-08 前是
  `len(_iter_leaf_items(entry))`——對整棵子樹做完整遞迴，算出「真正的葉節點總數」
  （例如 30 個子資料夾、每個 500 張圖，算出 15000）。**2026-09-08 改為**
  `_has_any_child()`＋`_count_immediate_children()`：
  - `_has_any_child(folder)`：單次 `os.scandir`，找到第一個非隱藏直接項目就
    `return True`，從不遞迴進子目錄——這是「這個分類是不是空的」判斷，成本從
    O(全部檔案) 降到 O(直接子項目數)。
  - `_count_immediate_children(folder)`：單次 `os.scandir`，只數直接子項目
    （檔案＋子目錄都各算 1 個，跟裡面裝了什麼／裝多少無關），不遞迴。
  - 結果：一個有 30 個子資料夾的一般模式來源，`item_count` 現在顯示 30，不是
    30×子資料夾內檔案數。**這是刻意的語意改變**（裁定 2026-09-07，見上方
    `qa_log`）——`item_count` 現在的意思是「這個分類底下有幾個直接項目」，不再是
    「遞迴數到底有幾張圖／幾支影片」。
  - **刻意沒做**：快取、分頁——裁定明確排除（「不快取不分頁」）。PM 分析裡提過的
    DB thread-local connection 方向也不在這次裁定範圍內，沒有做。
  - 前端 `GalleryView.vue` 的分類徽章（`cat-chip__count`）只顯示裸數字，沒有任何
    文字暗示「總圖片數」，這次沒有改前端文案。

### API 端點

- `GET /api/gallery`：`list_categories()`，各分類含 `item_count`。
- `GET /api/gallery/items?category=`：`list_items()`。
- `GET /api/gallery/files?path=`：`list_files()`。
- `GET /api/gallery/serve?p=`：`resolve_file()` 解析相對路徑（**含逃逸防護**——
  `resolved.is_relative_to(DOWNLOAD_DIR)`，拒絕 `../` 跳出下載目錄），再用
  Flask `send_file` 或**手動實作 HTTP Range 請求**（`Content-Range`/
  `Accept-Ranges` header，支援影片拖曳跳轉播放）串流回傳。

### 誠實現況

`tests/test_gallery_service.py` 覆蓋 `list_categories`/`list_items`/`list_files`
的目錄樹邏輯，含 2026-09-08 新增的一般模式直接子項目計數／非空早停測試（後者用
spy 包住 `os.scandir` 證明呼叫次數是 O(直接子項目)，不是 O(全部檔案)）；
`/api/gallery/serve` 的 Range 請求處理本身無獨立 pytest 覆蓋（僅 route 內邏輯
直觀，未見自動化驗證影片拖曳情境）。
