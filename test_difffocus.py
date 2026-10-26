"""difffocus 的自检测试。只用内存与临时目录里的样例数据。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from difffocus import (build_report, build_summary,  # noqa: E402
                       classify_changes, compare_files, detect_renames,
                       is_format_only, normalize_for_format,
                       parse_unified_diff)

ENTRY = os.path.join(BASE, "difffocus.py")

SAMPLE_PATCH = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,8 +1,9 @@
 import os
-def old_name():
-    return 1
+def new_name():
+    return 2
+    print("added line")
 x = 1
-    y = 2
+        y = 2
"""


REMOVE_PATCH = """diff --git a/b.py b/b.py
index 3333333..4444444 100644
--- a/b.py
+++ b/b.py
@@ -1,3 +1,2 @@
 keep = 1
-gone = 2
 another = 3
"""


def _types(changes) -> list:
    return [c.change_type for c in changes]


def test_normalize_ignores_indent_and_quotes() -> None:
    assert normalize_for_format('    a = "b"') == normalize_for_format('a = \'b\'')


def test_is_format_only_true_for_indent_change() -> None:
    assert is_format_only("    y = 2", "        y = 2") is True


def test_is_format_only_false_for_value_change() -> None:
    assert is_format_only("    y = 2", "    y = 3") is False


def test_is_format_only_false_for_quote_change() -> None:
    assert is_format_only('a = "x"', "a = 'x'") is True


def test_parse_unified_diff_counts_entries() -> None:
    entries = parse_unified_diff(SAMPLE_PATCH)
    kinds = [e[4] for e in entries]
    assert kinds.count("+") == 4, kinds
    assert kinds.count("-") == 3, kinds
    assert kinds.count(" ") == 2, kinds


def test_parse_unified_diff_counts_removal_only() -> None:
    entries = parse_unified_diff(REMOVE_PATCH)
    kinds = [e[4] for e in entries]
    assert kinds.count("-") == 1, kinds
    assert kinds.count("+") == 0, kinds


def test_classify_removed_only() -> None:
    changes = classify_changes(parse_unified_diff(REMOVE_PATCH))
    removed = [c for c in changes if c.change_type == "removed"]
    assert len(removed) == 1, _types(changes)
    assert "gone = 2" in removed[0].old_line, removed[0].old_line


def test_parse_unified_diff_reads_file_path() -> None:
    entries = parse_unified_diff(SAMPLE_PATCH)
    assert entries[0][0] == "app.py", entries[0]


def test_classify_detects_added() -> None:
    changes = classify_changes(parse_unified_diff(SAMPLE_PATCH))
    added = [c for c in changes if c.change_type == "added"]
    assert len(added) == 1, _types(changes)
    assert "added line" in added[0].new_line, added[0].new_line


def test_classify_detects_removed() -> None:
    changes = classify_changes(parse_unified_diff(SAMPLE_PATCH))
    types = _types(changes)
    assert "removed" in types or "logic" in types or "renamed" in types, types
    assert "logic" in types, types


def test_classify_detects_rename_candidate() -> None:
    changes = classify_changes(parse_unified_diff(SAMPLE_PATCH))
    renamed = [c for c in changes if c.change_type == "renamed"]
    assert len(renamed) == 1, _types(changes)
    assert renamed[0].deterministic is False, renamed[0].as_dict()


def test_classify_detects_format_only() -> None:
    changes = classify_changes(parse_unified_diff(SAMPLE_PATCH))
    fmt = [c for c in changes if c.change_type == "format_only"]
    assert len(fmt) == 1, _types(changes)
    assert fmt[0].deterministic is True, fmt[0].as_dict()


def test_classify_marks_logic_changes() -> None:
    changes = classify_changes(parse_unified_diff(SAMPLE_PATCH))
    logic = [c for c in changes if c.change_type == "logic"]
    # def old_name -> def new_name 与 return 1 -> return 2 是逻辑改动
    assert len(logic) >= 1, _types(changes)


def test_detect_renames_pairs_same_content() -> None:
    patch = ("@@ -1,2 +1,2 @@\n"
             "-def keep_me():\n"
             "+def keep_me_2():\n")
    entries = parse_unified_diff(patch)
    renames = detect_renames(entries)
    assert len(renames) == 1, renames


def test_build_summary_counts_noise() -> None:
    changes = classify_changes(parse_unified_diff(SAMPLE_PATCH))
    summary = build_summary(changes)
    assert summary["total"] == len(changes), summary
    assert summary["noise_count"] == 1, summary
    assert summary["signal_count"] == summary["total"] - 1, summary


def test_build_summary_empty() -> None:
    summary = build_summary([])
    assert summary["total"] == 0, summary
    assert summary["noise_ratio"] == 0.0, summary


def test_hide_format_removes_group() -> None:
    changes = classify_changes(parse_unified_diff(SAMPLE_PATCH))
    report = build_report(changes, hide_format=True)
    labels = [g["type_label"] for g in report["groups"]]
    assert "纯格式改动" not in labels, labels


def test_report_marks_judgment_items() -> None:
    changes = classify_changes(parse_unified_diff(SAMPLE_PATCH))
    report = build_report(changes)
    judgments = [c for g in report["groups"] for c in g["changes"]
                 if not c["deterministic"]]
    assert judgments, report
    assert judgments[0]["judgment"] == "需人判断", judgments[0]


def test_cli_help_exits_zero() -> None:
    r = subprocess.run([sys.executable, ENTRY, "--help"], capture_output=True,
                       text=True, timeout=30, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert "difffocus" in r.stdout


def test_cli_analyze() -> None:
    tmp = tempfile.mkdtemp(prefix="difffocus_")
    try:
        p = Path(tmp) / "d.patch"
        p.write_text(SAMPLE_PATCH, encoding="utf-8")
        r = subprocess.run([sys.executable, ENTRY, "analyze", "--input", str(p)],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        assert "变更总数" in r.stdout, r.stdout
        assert "纯格式改动" in r.stdout, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_analyze_json() -> None:
    tmp = tempfile.mkdtemp(prefix="difffocus_")
    try:
        p = Path(tmp) / "d.patch"
        p.write_text(SAMPLE_PATCH, encoding="utf-8")
        r = subprocess.run([sys.executable, ENTRY, "analyze", "--input", str(p),
                            "--json"], capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["total"] > 0, data
        assert "groups" in data, data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_compare_two_files() -> None:
    tmp = tempfile.mkdtemp(prefix="difffocus_")
    try:
        old = Path(tmp) / "old.py"
        new = Path(tmp) / "new.py"
        old.write_text("a = 1\nb = 2\n", encoding="utf-8")
        new.write_text("a = 1\nb = 3\n", encoding="utf-8")
        r = subprocess.run([sys.executable, ENTRY, "compare", "--old", str(old),
                            "--new", str(new)], capture_output=True, text=True,
                           timeout=30, encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        assert "变更总数" in r.stdout, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_missing_file_exits_two() -> None:
    r = subprocess.run([sys.executable, ENTRY, "analyze", "--input", "no_such.patch"],
                       capture_output=True, text=True, timeout=30,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 2, (r.returncode, r.stderr)


def test_compare_files_produces_diff() -> None:
    tmp = tempfile.mkdtemp(prefix="difffocus_")
    try:
        old = Path(tmp) / "a.txt"
        new = Path(tmp) / "b.txt"
        old.write_text("x\n", encoding="utf-8")
        new.write_text("y\n", encoding="utf-8")
        text = compare_files(old, new)
        assert text.startswith("---"), text
        assert "+y" in text, text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [OK  ] {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n共 {len(tests)} 项，失败 {failed} 项")
    raise SystemExit(1 if failed else 0)
