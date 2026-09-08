"""
tests/test_gallery_service.py

覆蓋 gallery_service 的安全關鍵路徑：
- resolve_file：path traversal 防護（is_relative_to 邊界）
- list_items / list_files：正常功能
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import gallery_service


# ──────────────────────────────────────────────────────────
# resolve_file — path traversal 防護
# ──────────────────────────────────────────────────────────


class TestResolveFile:
    """resolve_file 必須把所有路徑限制在 DOWNLOAD_DIR 內。"""

    def test_valid_file_returns_path(self, tmp_download_dir: Path):
        target = tmp_download_dir / "img.jpg"
        target.write_bytes(b"fake image")
        result = gallery_service.resolve_file("img.jpg")
        assert result == target.resolve()

    def test_nonexistent_file_returns_none(self, tmp_download_dir: Path):
        result = gallery_service.resolve_file("nonexistent.jpg")
        assert result is None

    def test_directory_returns_none(self, tmp_download_dir: Path):
        (tmp_download_dir / "subdir").mkdir()
        result = gallery_service.resolve_file("subdir")
        assert result is None

    def test_path_traversal_dotdot_returns_none(self, tmp_download_dir: Path):
        """../../etc/passwd 不能逃出 DOWNLOAD_DIR。"""
        result = gallery_service.resolve_file("../../etc/passwd")
        assert result is None

    def test_path_traversal_encoded_returns_none(self, tmp_download_dir: Path):
        """多層 .. 組合仍不能逃出目錄。"""
        result = gallery_service.resolve_file("subdir/../../../etc/shadow")
        assert result is None

    def test_absolute_path_traversal_returns_none(self, tmp_download_dir: Path):
        """傳入絕對路徑也必須被擋住（除非它本來就在 DOWNLOAD_DIR 內）。"""
        result = gallery_service.resolve_file("/etc/passwd")
        assert result is None

    def test_nested_valid_file_returns_path(self, tmp_download_dir: Path):
        sub = tmp_download_dir / "pixiv" / "artist"
        sub.mkdir(parents=True)
        f = sub / "image.png"
        f.write_bytes(b"data")
        result = gallery_service.resolve_file("pixiv/artist/image.png")
        assert result == f.resolve()


# ──────────────────────────────────────────────────────────
# list_categories / list_items / list_files
# ──────────────────────────────────────────────────────────


class TestListCategories:
    def test_empty_dir_returns_empty(self, tmp_download_dir: Path):
        assert gallery_service.list_categories() == []

    def test_returns_category_names(self, tmp_download_dir: Path):
        discord = tmp_download_dir / "discord"
        discord.mkdir()
        (discord / "video.mp4").write_bytes(b"v")
        pixiv = tmp_download_dir / "pixiv"
        pixiv.mkdir()
        (pixiv / "artist" / "work").mkdir(parents=True)
        (pixiv / "artist" / "work" / "1.jpg").write_bytes(b"i")
        cats = gallery_service.list_categories()
        names = [c["name"] for c in cats]
        assert "discord" in names
        assert "pixiv" in names

    def test_hidden_dirs_excluded(self, tmp_download_dir: Path):
        hidden = tmp_download_dir / ".hidden"
        hidden.mkdir()
        (hidden / "file.jpg").write_bytes(b"x")  # would count if not hidden-filtered
        cats = gallery_service.list_categories()
        assert all(c["name"] != ".hidden" for c in cats)

    def test_empty_category_is_filtered_out(self, tmp_download_dir: Path):
        """使用者要求：空資料夾不用刪，但顯示要過濾 0 的 —— 一律用 item_count == 0
        判斷，不新增任何額外掃描（見 gallery_service.list_categories docstring）。"""
        (tmp_download_dir / "nhentai.net").mkdir()  # real leftover example
        (tmp_download_dir / "youtube").mkdir()
        cats = gallery_service.list_categories()
        names = [c["name"] for c in cats]
        assert "nhentai.net" not in names
        assert "youtube" not in names
        # the folders themselves must still exist on disk — never deleted
        assert (tmp_download_dir / "nhentai.net").is_dir()
        assert (tmp_download_dir / "youtube").is_dir()

    def test_category_with_items_is_shown_alongside_empty_ones(self, tmp_download_dir: Path):
        (tmp_download_dir / "empty_leftover").mkdir()
        real = tmp_download_dir / "bahamut"
        real.mkdir()
        (real / "clip.mp4").write_bytes(b"v")
        cats = gallery_service.list_categories()
        names = [c["name"] for c in cats]
        assert "bahamut" in names
        assert "empty_leftover" not in names

    def test_doujinshi_category_item_count_is_direct_subfolder_count(self, tmp_download_dir: Path):
        """Root-cause regression: item_count for a doujinshi source must be
        "one subfolder = one book" — the SAME definition list_source_books
        uses — not the general-mode leaf-recursion count, even when a book
        folder itself contains a nested subfolder (which the leaf-recursion
        definition would silently exclude or split apart)."""
        wnacg = tmp_download_dir / "wnacg"
        wnacg.mkdir()
        flat_book = wnacg / "100_flat_book"
        flat_book.mkdir()
        (flat_book / "001.jpg").write_bytes(b"i")
        nested_book = wnacg / "200_book_with_bonus_subfolder"
        nested_book.mkdir()
        (nested_book / "001.jpg").write_bytes(b"i")
        (nested_book / "bonus").mkdir()
        (nested_book / "bonus" / "extra.jpg").write_bytes(b"i")

        cats = gallery_service.list_categories()
        wnacg_cat = next(c for c in cats if c["name"] == "wnacg")
        assert wnacg_cat["item_count"] == 2
        assert wnacg_cat["mode"] == "doujinshi"

    def test_general_category_item_count_is_direct_children_count(self, tmp_download_dir: Path):
        """裁定 2026-09-07（@PM 待回答 #53 item 3(b)）：一般模式來源的
        item_count = 直接子項目數，不是遞迴葉節點總數。30 個子資料夾、每個
        50 個檔案，必須顯示 30，不是 30*50=1500——用縮小過的檔案數（原始裁定
        舉例是 500）換取測試速度，比例關係不變，足以證明沒有遞迴進子資料夾。"""
        cat = tmp_download_dir / "bigsource"
        cat.mkdir()
        for i in range(30):
            sub = cat / f"B{i:02d}"
            sub.mkdir()
            for j in range(50):
                (sub / f"{j:03d}.jpg").write_bytes(b"i")
        cats = gallery_service.list_categories()
        bigsource = next(c for c in cats if c["name"] == "bigsource")
        assert bigsource["item_count"] == 30
        assert bigsource["mode"] == "general"

    def test_general_non_empty_check_stops_at_first_item_no_full_walk(self, tmp_download_dir: Path):
        """裁定 2026-09-07（@PM 待回答 #53 item 3(a)）：一般模式的「這個分類
        是不是非空」判斷必須在找到第一個項目就停，不能遞迴進子資料夾做全樹
        掃描。這裡把唯一的檔案埋在兩層子資料夾底下（cat/sub1/sub2/deep.jpg），
        用 spy 包住 os.scandir 證明：整個 list_categories() 呼叫期間，
        scandir 只對 `cat` 本身呼叫過（一次來自非空檢查、一次來自直接子項目
        計數），從未進入 sub1 或 sub1/sub2 —— 這正是「不是 O(全部檔案)，而是
        O(直接子項目)」的證據，而不只是最後數字剛好對了。"""
        cat = tmp_download_dir / "deepsource"
        cat.mkdir()
        nested = cat / "sub1" / "sub2"
        nested.mkdir(parents=True)
        (nested / "deep.jpg").write_bytes(b"i")

        real_scandir = os.scandir
        calls: list[Path] = []

        def _spy_scandir(path, *args, **kwargs):
            calls.append(Path(path))
            return real_scandir(path, *args, **kwargs)

        with patch("app.services.gallery_service.os.scandir", side_effect=_spy_scandir):
            cats = gallery_service.list_categories()

        deepsource = next(c for c in cats if c["name"] == "deepsource")
        assert deepsource["item_count"] == 1  # only "sub1" is a direct child
        assert calls == [cat, cat]  # never scanned sub1/ or sub1/sub2/


class TestListItems:
    def test_unknown_category_returns_empty(self, tmp_download_dir: Path):
        assert gallery_service.list_items("nonexistent") == []

    def test_leaf_dir_returned_as_dir_item(self, tmp_download_dir: Path):
        leaf = tmp_download_dir / "gallery" / "work1"
        leaf.mkdir(parents=True)
        (leaf / "a.jpg").write_bytes(b"img")
        items = gallery_service.list_items("gallery")
        assert any(i["name"] == "work1" and i["type"] == "dir" for i in items)

    def test_standalone_media_file_returned(self, tmp_download_dir: Path):
        cat = tmp_download_dir / "ytdlp"
        cat.mkdir()
        (cat / "video.mp4").write_bytes(b"v")
        items = gallery_service.list_items("ytdlp")
        assert any(i["name"] == "video.mp4" and i["type"] == "file" for i in items)


class TestListFiles:
    def test_single_file_item(self, tmp_download_dir: Path):
        f = tmp_download_dir / "solo.jpg"
        f.write_bytes(b"img")
        files = gallery_service.list_files("solo.jpg")
        assert len(files) == 1
        assert files[0]["name"] == "solo.jpg"

    def test_dir_item_lists_contents(self, tmp_download_dir: Path):
        folder = tmp_download_dir / "album"
        folder.mkdir()
        (folder / "1.jpg").write_bytes(b"a")
        (folder / "2.png").write_bytes(b"b")
        files = gallery_service.list_files("album")
        names = [f["name"] for f in files]
        assert "1.jpg" in names
        assert "2.png" in names
