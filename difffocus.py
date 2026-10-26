"""difffocus —— 把版本差异按语义分类，让真实改动浮出来。

设计原则：
1. 按语义分类，不按行分类：新增、删除、重命名、纯格式、逻辑改动分开列。
2. 噪音单独成组并可一键收起，不让用户逐条排除。
3. 机械可判定的直接判，需要人判断的标出来但不替人决定。

仅依赖 Python 标准库。

    python difffocus.py --help
    python difffocus.py analyze --input diff.patch
    python difffocus.py analyze --input diff.patch --hide-format
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

VERSION = "0.1.0"

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

# 变更类型
CHANGE_TYPES = {
    "added": "新增",
    "removed": "删除",
    "renamed": "重命名",
    "format_only": "纯格式改动",
    "logic": "逻辑改动",
    "context": "上下文",
}

# 机械可判定 / 需要人判断
DETERMINISTIC = "可机械判定"
JUDGMENT = "需人判断"


class Change:
    """一条被归类后的变更。"""

    def __init__(self, change_type: str, old_line: str, new_line: str,
                 file_path: str, hunk: str, deterministic: bool = True,
                 note: str = "") -> None:
        self.change_type = change_type
        self.old_line = old_line
        self.new_line = new_line
        self.file_path = file_path
        self.hunk = hunk
        self.deterministic = deterministic
        self.note = note

    def as_dict(self) -> dict:
        return {
            "type": self.change_type,
            "type_label": CHANGE_TYPES.get(self.change_type, self.change_type),
            "file": self.file_path,
            "hunk": self.hunk,
            "old_line": self.old_line,
            "new_line": self.new_line,
            "deterministic": self.deterministic,
            "judgment": DETERMINISTIC if self.deterministic else JUDGMENT,
            "note": self.note,
        }


def normalize_for_format(line: str) -> str:
    """把一行文本归一化成「去掉格式差异」的形式。

    处理：缩进宽度、引号风格、行尾空白。
    """
    text = line.strip()
    text = re.sub(r'["\u201c\u201d]', "'", text)
    text = re.sub(r"\s+", " ", text)
    return text


def is_format_only(old_line: str, new_line: str) -> bool:
    """判断一对文本是否只有格式差异。

    保守判定：归一化之后完全相同才算纯格式。
    """
    return normalize_for_format(old_line) == normalize_for_format(new_line)


def parse_unified_diff(text: str) -> list:
    """解析 unified diff，返回 (文件路径, hunk 头, 旧行号, 新行号, 行类型, 内容) 列表。"""
    entries = []
    current_file = ""
    hunk_header = ""
    old_no = new_no = 0
    for raw in text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            current_file = path
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("diff --git"):
            current_file = ""
            continue
        m = HUNK_RE.match(raw)
        if m:
            hunk_header = raw
            old_no = int(m.group(1))
            new_no = int(m.group(3))
            continue
        if raw.startswith("+"):
            entries.append((current_file, hunk_header, old_no, new_no, "+", raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            entries.append((current_file, hunk_header, old_no, new_no, "-", raw[1:]))
            old_no += 1
        elif raw.startswith(" "):
            entries.append((current_file, hunk_header, old_no, new_no, " ", raw[1:]))
            old_no += 1
            new_no += 1
    return entries


def _tokens(text: str) -> list:
    """把一行拆成词法单元，用于识别标识符变化。"""
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]", text.strip())


def _is_identifier(token: str) -> bool:
    return bool(token) and (token[0].isalpha() or token[0] == "_")


def _is_rename_pair(old_line: str, new_line: str) -> bool:
    """判断一对文本是否可能是重命名。

    判据：词法结构相同，只有**一个**位置的标识符不同。
    例如 ``def old_name():`` 与 ``def new_name():``。

    这只是候选——重命名与「改了标识符名的逻辑改动」在文本上无法区分，
    所以结果标记为需人判断。
    """
    old_tokens = _tokens(old_line)
    new_tokens = _tokens(new_line)
    if len(old_tokens) != len(new_tokens) or not old_tokens:
        return False
    diffs = [(a, b) for a, b in zip(old_tokens, new_tokens) if a != b]
    if len(diffs) != 1:
        return False
    old_token, new_token = diffs[0]
    return _is_identifier(old_token) and _is_identifier(new_token)


def detect_renames(entries: list) -> dict:
    """找出「删一行、加一行」的重命名候选。

    返回 旧行下标 → 新行下标 的映射。
    只能判定「文本形态上的对应」，不能判定语义上的重命名，所以调用方
    必须把它标成需人判断。
    """
    renames = {}
    by_hunk: dict = {}
    for index, (file_path, hunk, old_no, new_no, kind, content) in enumerate(entries):
        if kind == " ":
            continue
        by_hunk.setdefault((file_path, hunk), []).append(index)

    for indices in by_hunk.values():
        removed = [i for i in indices if entries[i][4] == "-"]
        added = [i for i in indices if entries[i][4] == "+"]
        used_added = set()
        for ri in removed:
            for ai in added:
                if ai in used_added:
                    continue
                old_text = entries[ri][5]
                new_text = entries[ai][5]
                if old_text.strip() == new_text.strip():
                    if old_text.rstrip() == new_text.rstrip():
                        # 内容被原样删掉又加回来：可能是移动
                        renames[ri] = ai
                        used_added.add(ai)
                        break
                    # 只有缩进差异，属于格式改动，不算重命名
                    continue
                if _is_rename_pair(old_text, new_text):
                    renames[ri] = ai
                    used_added.add(ai)
                    break
    return renames


def _blocks(entries: list) -> list:
    """把 diff 条目切成「连续变更块」。

    上下文行（以空格开头）是块的分隔符：
    同一块里的删除与新增属于同一处改写。
    """
    blocks: list = []
    current: list = []
    for index, entry in enumerate(entries):
        if entry[4] == " ":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(index)
    if current:
        blocks.append(current)
    return blocks


def _match_block(entries: list, block: list) -> list:
    """在一个变更块里，把删除行与新增行配对。

    配对依据是文本相似度（difflib 的 ratio）。
    相似度过低的行不配对——它们是独立的删除与新增。
    """
    removed = [i for i in block if entries[i][4] == "-"]
    added = [i for i in block if entries[i][4] == "+"]
    if not removed or not added:
        return []

    scored = []
    for ri in removed:
        for ai in added:
            ratio = difflib.SequenceMatcher(
                None, entries[ri][5].strip(), entries[ai][5].strip()).ratio()
            if ratio >= 0.5:
                scored.append((ratio, ri, ai))
    scored.sort(reverse=True)

    used_removed = set()
    used_added = set()
    pairs = []
    for ratio, ri, ai in scored:
        if ri in used_removed or ai in used_added:
            continue
        used_removed.add(ri)
        used_added.add(ai)
        pairs.append((ri, ai))
    return pairs


def classify_changes(entries: list) -> list:
    """把 diff 条目归类成变更列表。

    配对规则：在同一个变更块里，把内容相似的「删除 + 新增」视为
    **同一行被改写**，再按内容细分：
      - 归一化后相同     → 纯格式改动
      - 只差一个标识符   → 重命名候选（需人判断）
      - 内容确有变化     → 逻辑改动
    未配对的「新增」单独算新增，未配对的「删除」单独算删除。
    """
    renames = detect_renames(entries)
    rename_sources = set(renames.keys())

    changes: list[Change] = []
    for block in _blocks(entries):
        pairs = _match_block(entries, block)
        paired_removed = {ri for ri, _ in pairs}
        paired_added = {ai for _, ai in pairs}

        for ri, ai in pairs:
            file_path = entries[ri][0]
            hunk = entries[ri][1]
            old_text = entries[ri][5]
            new_text = entries[ai][5]
            if ri in renames:
                changes.append(Change(
                    "renamed", old_text, new_text, file_path, hunk,
                    deterministic=False,
                    note="同一块内删增内容形态一致，可能是移动或重命名——需人工确认"))
            elif is_format_only(old_text, new_text):
                changes.append(Change(
                    "format_only", old_text, new_text, file_path, hunk,
                    deterministic=True,
                    note="仅缩进、引号风格或空白差异"))
            else:
                changes.append(Change(
                    "logic", old_text, new_text, file_path, hunk,
                    deterministic=True,
                    note="同一行被改写，内容确有变化"))

        for index in block:
            kind = entries[index][4]
            file_path = entries[index][0]
            hunk = entries[index][1]
            content = entries[index][5]
            if kind == "+":
                if index in paired_added:
                    continue
                changes.append(Change("added", "", content, file_path, hunk))
            else:
                if index in paired_removed:
                    continue
                if index in rename_sources:
                    continue
                changes.append(Change("removed", content, "", file_path, hunk))
    return changes


def build_summary(changes: list) -> dict:
    """按类型分组统计。"""
    groups: dict = {}
    for change in changes:
        groups.setdefault(change.change_type, []).append(change)
    order = ["logic", "added", "removed", "renamed", "format_only"]
    ordered = []
    for key in order:
        if key in groups:
            ordered.append((key, groups[key]))
    for key in groups:
        if key not in order:
            ordered.append((key, groups[key]))

    counts = {CHANGE_TYPES.get(k, k): len(v) for k, v in ordered}
    total = len(changes)
    noise = len(groups.get("format_only", []))
    signal = total - noise
    return {
        "total": total,
        "signal_count": signal,
        "noise_count": noise,
        "noise_ratio": (noise / total) if total else 0.0,
        "counts": counts,
        "groups": ordered,
    }


def build_report(changes: list, hide_format: bool = False,
                 source: str = "") -> dict:
    summary = build_summary(changes)
    grouped = []
    for key, items in summary["groups"]:
        if hide_format and key == "format_only":
            continue
        grouped.append({
            "type": key,
            "type_label": CHANGE_TYPES.get(key, key),
            "count": len(items),
            "changes": [c.as_dict() for c in items],
        })
    return {
        "version": VERSION,
        "source": source,
        "total": summary["total"],
        "signal_count": summary["signal_count"],
        "noise_count": summary["noise_count"],
        "noise_ratio": round(summary["noise_ratio"], 4),
        "counts": summary["counts"],
        "groups": grouped,
    }


def render_text(report: dict, hide_format: bool = False) -> str:
    lines = []
    lines.append(f"变更总数：{report['total']}")
    lines.append(f"有效变更：{report['signal_count']}")
    lines.append(f"格式噪音：{report['noise_count']}"
                 f"（占 {report['noise_ratio'] * 100:.1f}%）")
    lines.append("")
    for label, count in report["counts"].items():
        lines.append(f"  {label}：{count}")
    lines.append("")
    if report["noise_ratio"] >= 0.5 and report["total"]:
        lines.append("提示：格式改动占比过半，建议先用 --hide-format 收起来看一遍。")
        lines.append("")
    for group in report["groups"]:
        lines.append(f"【{group['type_label']}】{group['count']} 条")
        for change in group["changes"]:
            marker = " " if change["deterministic"] else "?"
            lines.append(f"  {marker} {change['file']} {change['hunk']}")
            if change["type"] == "added":
                lines.append(f"      + {change['new_line'].rstrip()}")
            elif change["type"] == "removed":
                lines.append(f"      - {change['old_line'].rstrip()}")
            elif change["type"] == "renamed":
                lines.append(f"      ~ {change['old_line'].rstrip()}")
            else:
                lines.append(f"      - {change['old_line'].rstrip()}")
                lines.append(f"      + {change['new_line'].rstrip()}")
            if not change["deterministic"]:
                lines.append(f"      （{change['judgment']}）{change['note']}")
        lines.append("")
    lines.append("说明：")
    lines.append("  带 ? 标记的是机械规则无法确定的条目，工具只标出、不替你下结论。")
    lines.append("  「纯格式改动」的判定是保守的：归一化后必须完全一致才算格式改动。")
    return "\n".join(lines)


def compare_files(old_path: Path, new_path: Path) -> str:
    """生成两份文本文件的 unified diff，供 analyze 使用。"""
    old_lines = old_path.read_text(encoding="utf-8", errors="replace").splitlines()
    new_lines = new_path.read_text(encoding="utf-8", errors="replace").splitlines()
    diff = difflib.unified_diff(old_lines, new_lines,
                                fromfile=f"a/{old_path.name}",
                                tofile=f"b/{new_path.name}", lineterm="")
    return "\n".join(diff)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="difffocus",
        description="把版本差异按语义分类：新增/删除/重命名/纯格式/逻辑改动分开列",
        epilog=(
            "示例:\n"
            "  python difffocus.py analyze --input diff.patch\n"
            "  python difffocus.py analyze --input diff.patch --hide-format\n"
            "  python difffocus.py analyze --input diff.patch --json\n"
            "  python difffocus.py compare --old old.py --new new.py\n"
            "\n"
            "「纯格式改动」判定保守：归一化后完全一致才算格式改动。\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"difffocus {VERSION}")
    sub = ap.add_subparsers(dest="command")

    ana = sub.add_parser("analyze", help="分析 unified diff")
    ana.add_argument("--input", required=True, help="diff 文件路径")
    ana.add_argument("--hide-format", action="store_true",
                     help="把纯格式改动从主视图里收起来")
    ana.add_argument("--json", action="store_true", help="输出 JSON")

    cmp_ = sub.add_parser("compare", help="比较两个文本文件")
    cmp_.add_argument("--old", required=True, help="旧文件路径")
    cmp_.add_argument("--new", required=True, help="新文件路径")
    cmp_.add_argument("--hide-format", action="store_true", help="收起格式改动")
    cmp_.add_argument("--json", action="store_true", help="输出 JSON")

    args = ap.parse_args(argv)
    if not args.command:
        ap.print_help()
        return 0

    if args.command == "analyze":
        path = Path(args.input)
        if not path.is_file():
            print(f"找不到 diff 文件：{path}", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8", errors="replace")
        entries = parse_unified_diff(text)
        changes = classify_changes(entries)
        report = build_report(changes, args.hide_format, str(path))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(render_text(report, args.hide_format))
        return 0

    if args.command == "compare":
        old = Path(args.old)
        new = Path(args.new)
        if not old.is_file() or not new.is_file():
            print("找不到要比对的文件。", file=sys.stderr)
            return 2
        text = compare_files(old, new)
        entries = parse_unified_diff(text)
        changes = classify_changes(entries)
        report = build_report(changes, args.hide_format,
                              f"{old} -> {new}")
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(render_text(report, args.hide_format))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
