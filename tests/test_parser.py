# -*- coding: utf-8 -*-
import os, re, sys, subprocess
from pathlib import Path
import psycopg2

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/postgres")
os.environ["DATABASE_URL"] = TEST_DB_URL
REPO   = Path(__file__).parent.parent
SEED   = Path(__file__).parent / "seed.sql"
CORPUS = Path(__file__).parent / "test_corpus.txt"
REPORT = Path(__file__).parent / "test_report.txt"
COMMIT = "7f2d912"
sys.path.insert(0, str(REPO))

import services
from commands import handle_admin, handle_finance, handle_amend_last

ANCHOR = re.compile(r"^--- (EDGE|ALL)-(\d+) \[")

def full_reset():
    truncate_sql = (
        b"DO $$ DECLARE r RECORD; BEGIN "
        b"FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
        b"EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' RESTART IDENTITY CASCADE'; "
        b"END LOOP; END $$;\n"
    )
    combined = truncate_sql + SEED.read_bytes()
    result = subprocess.run(
        ["docker", "exec", "-i", "ahab-test-db", "psql", "-U", "postgres", "-d", "postgres"],
        input=combined, capture_output=True,
    )
    if result.returncode != 0:
        sys.exit("Seed failed:\n" + result.stderr.decode(errors="replace"))
    print("DB reset + seed OK")

# Router -- codepoints verified via ascii() on app.py
_AMEND_SW = ("\u6539\u50f9", "\u6539\u91d1\u984d", "\u5099\u8a3b", "\u7b46\u8a18")
_ADMIN_SW = ("\u65b0\u589e", "\u8a2d\u5b9a", "\u522a\u9664",
             "\u6aa2\u67e5\u7f3a\u6f0f", "\u4e00\u9375\u88dc\u5e7d\u9748",
             "\u62c6\u5206", "\u5408\u4f75", "\u6e05\u7a7a\u6708\u4efd",
             "\u6e05\u9664\u5e7d\u9748")
_ADMIN_EQ = {"\u6e05\u9664\u7570\u5e38", "\u4eba\u54e1\u540d\u55ae"}
_FIN_SW   = ("\u532f\u51fa", "\u7d50\u7b97", "\u767e\u8ca8", "\u6a94\u671f\u7d50\u7b97")
_FIN_EQ   = {"\u50f9\u76ee\u8868", "\u6e05\u55ae", "\u7d71\u8a08",
             "\u5831\u8868", "\u660e\u7d30", "\u5b8c\u6574"}
_MON_RE   = re.compile(r"^\d+\u6708(\u5831\u8868|\u660e\u7d30|\u5b8c\u6574)")
_HELP_EQ  = {"help", "\u5e6b\u52a9", "\u6307\u4ee4"}

def dispatch(msg):
    if "\n" in msg and re.search(r"\d+[/-]\d+", msg):
        return "BATCH", None
    if msg in _HELP_EQ:
        return "HELP", None
    if any(msg.startswith(p) for p in _AMEND_SW):
        return "AMEND", handle_amend_last
    if any(msg.startswith(p) for p in _ADMIN_SW) or msg in _ADMIN_EQ:
        return "ADMIN", handle_admin
    if any(msg.startswith(p) for p in _FIN_SW) or msg in _FIN_EQ or _MON_RE.match(msg):
        return "FINANCE", handle_finance
    if re.search(r"\d+[/-]\d+", msg):
        return "RECORD", services.handle_record_expense_smart
    return "NOMATCH", None

def parse_corpus(path):
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    anchors = []
    for i, line in enumerate(lines):
        m = ANCHOR.match(line)
        if m:
            anchors.append((i, m.group(1), int(m.group(2))))
    entries = []
    for idx, (pos, kind, num) in enumerate(anchors):
        end = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(lines)
        block = lines[pos:end]
        inp_lines, old_lines, section = [], [], None
        for line in block[1:]:
            s = line.rstrip("\n")
            if s == "INPUT:":
                section = "inp"
            elif s == "OLD_OUTPUT:":
                section = "old"
            elif section == "inp":
                inp_lines.append(s)
            elif section == "old":
                old_lines.append(s)
        while old_lines and not old_lines[-1].strip():
            old_lines.pop()
        entries.append({
            "kind": kind, "num": num,
            "label": f"{kind}-{num:03d}",
            "input": "\n".join(inp_lines).strip(),
            "old_output": "\n".join(old_lines).strip(),
        })
    return entries

# Classifier
_P_NEW = ("\u627e\u4e0d\u5230", "\u6211\u5148\u6c92\u8a18", "\u8acb\u5148\u5efa\u6a94")
_P_OLD = ("\u5b8c\u6210", "\u7d00\u9304 ")
_ADM_PX = ("\u522a\u9664", "\u8a2d\u5b9a", "\u65b0\u589e", "\u5408\u4f75")
_C0     = "\u7e3d\u6210\u672c: 0"
_CNZ    = re.compile(r"\u7e3d\u6210\u672c: [1-9]")

def _extract_loc(s):
    for line in s.splitlines():
        if line.startswith("\u2705"):
            parts = line.split()
            if len(parts) >= 4:
                return parts[2]
    return None

def _extract_members(s):
    for line in s.splitlines():
        if "\u6210\u54e1:" in line:
            idx = line.index("\u6210\u54e1:") + 3
            return frozenset(m.strip() for m in line[idx:].strip().split(","))
    return None

def classify_diff(inp, old, new):
    if any(s in new for s in _P_NEW) and any(s in old for s in _P_OLD):
        return "PATCH"
    if old.startswith("\u2705") and new.startswith("\u2705"):
        loc_old, loc_new = _extract_loc(old), _extract_loc(new)
        mem_old, mem_new = _extract_members(old), _extract_members(new)
        if (loc_old is not None and loc_old == loc_new
                and mem_old is not None and mem_old == mem_new
                and re.sub(r"\d+", "X", old) == re.sub(r"\d+", "X", new)):
            return "STATE-DRIFT"
        return "UNKNOWN"
    if old.startswith("\u2705") and inp.startswith(_ADM_PX):
        return "MISALIGN"
    if (_C0 in old and _CNZ.search(new)) or (_CNZ.search(old) and _C0 in new):
        return "STATE-DRIFT"
    return "UNKNOWN"

def main():
    full_reset()
    entries   = parse_corpus(CORPUS)
    all_e     = [e for e in entries if e["kind"] == "ALL"]
    edge_nums = {e["num"] for e in entries if e["kind"] == "EDGE"}
    print(f"Corpus: ALL={len(all_e)} EDGE-nums={len(edge_nums)}")
    consistent = 0
    diffs, skipped_list = [], []
    for e in all_e:
        route_tag, fn = dispatch(e["input"])
        if fn is None:
            skipped_list.append((e["label"], route_tag, e["input"]))
            continue
        try:
            actual = fn(e["input"])
            actual = str(actual).strip() if actual is not None else "(None)"
        except Exception as ex:
            actual = f"(EXCEPTION: {ex})"
        if actual == e["old_output"]:
            consistent += 1
        else:
            cat = classify_diff(e["input"], e["old_output"], actual)
            diffs.append({**e, "actual": actual, "category": cat,
                          "is_edge": e["num"] in edge_nums, "route": route_tag})
    diffs.sort(key=lambda d: (not d["is_edge"], d["category"], d["num"]))
    cat_counts = {}
    for d in diffs:
        cat_counts[d["category"]] = cat_counts.get(d["category"], 0) + 1
    out = [
        f"=== Ahab Parser Replay Report  [Router mirrored from app.py @ {COMMIT}] ===",
        f"ALL entries : {len(all_e)}  skipped(BATCH/HELP/NOMATCH): {len(skipped_list)}",
        f"Replayed    : {len(all_e) - len(skipped_list)}",
        f"Consistent  : {consistent}",
        f"Divergent   : {len(diffs)}",
    ]
    for cat in ("PATCH", "STATE-DRIFT", "MISALIGN", "UNKNOWN"):
        out.append(f"  {cat:<12}: {cat_counts.get(cat, 0)}")
    out.append("")
    for d in diffs:
        etag = " [EDGE]" if d["is_edge"] else ""
        out.append(f"--- {d['label']}{etag} [{d['category']}] [{d['route']}] ---")
        out.append(f"INPUT  : {ascii(d['input'])}")
        out.append(f"OLD    : {ascii(d['old_output'])}")
        out.append(f"ACTUAL : {ascii(d['actual'])}")
        out.append("")
    out.append("=== ROUTE-SKIP list ===")
    for label, tag, inp in skipped_list:
        snippet = ascii(inp.replace("\n", " ")[:40])
        out.append(f"{label}  [{tag}]  {snippet}")
    REPORT.write_text("\n".join(out), encoding="utf-8")
    print(f"Consistent {consistent} / Divergent {len(diffs)} / Skipped {len(skipped_list)}")
    print(f"Report: {REPORT}")

if __name__ == "__main__":
    main()
