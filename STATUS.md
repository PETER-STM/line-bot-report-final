# STATUS
- Change: 修 services.py 非確定性：final_members 改 sorted、locations SELECT 加 ORDER BY；不碰 294 行成員匹配邏輯（留 B-2）
- py_compile: PASS (services.py)
- Test: 四跑穩定 422/690（改前 426/421/419/418 變異 8）
