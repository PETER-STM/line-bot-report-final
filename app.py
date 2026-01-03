import os
import re
import calendar
from datetime import datetime, date, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import psycopg2
from psycopg2 import pool

# --- 1. 環境變數與設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
DATABASE_URL = os.getenv('DATABASE_URL')
COMPANY_NAME = os.getenv('COMPANY_NAME', '公司')

app = Flask(__name__)

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and DATABASE_URL):
    app.logger.error("❌ Key variables missing")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 2. 資料庫連接池 ---
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, DATABASE_URL, sslmode='require')
    if db_pool: app.logger.info("✅ DB Pool Ready")
except Exception as e:
    app.logger.error(f"❌ Pool Error: {e}")
    db_pool = None

def get_db_connection():
    return db_pool.getconn() if db_pool else psycopg2.connect(DATABASE_URL, sslmode='require')

def close_db_connection(conn):
    if db_pool and conn: db_pool.putconn(conn)
    elif conn: conn.close()

def init_db(force_recreate=False):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            if force_recreate:
                tables = ["records", "project_members", "projects", "monthly_settlements", 
                          "locations", "monthly_items", "members"]
                for t in tables: cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE;")
            
            cur.execute("CREATE TABLE IF NOT EXISTS monthly_items (item_name VARCHAR(50) PRIMARY KEY, default_members TEXT NOT NULL, memo TEXT);")
            cur.execute("CREATE TABLE IF NOT EXISTS locations (location_name VARCHAR(50) PRIMARY KEY, weekday_cost INTEGER NOT NULL, open_days TEXT, linked_monthly_item VARCHAR(50) REFERENCES monthly_items(item_name) ON DELETE SET NULL);")
            cur.execute("CREATE TABLE IF NOT EXISTS members (name VARCHAR(50) PRIMARY KEY);")
            cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
            cur.execute("CREATE TABLE IF NOT EXISTS projects (project_id UUID DEFAULT uuid_generate_v4() PRIMARY KEY, record_date DATE NOT NULL, location_name VARCHAR(50) REFERENCES locations(location_name) ON DELETE RESTRICT, total_fixed_cost INTEGER NOT NULL, original_msg TEXT);")
            cur.execute("CREATE TABLE IF NOT EXISTS monthly_settlements (id SERIAL PRIMARY KEY, item_name VARCHAR(50) REFERENCES monthly_items(item_name) ON DELETE RESTRICT, settlement_date DATE NOT NULL, cost_amount INTEGER NOT NULL, actual_members TEXT NOT NULL, total_capacity INTEGER NOT NULL, original_msg TEXT, UNIQUE (settlement_date, item_name));")
            cur.execute("CREATE TABLE IF NOT EXISTS project_members (project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE, member_name VARCHAR(50) REFERENCES members(name) ON DELETE CASCADE, PRIMARY KEY (project_id, member_name));")
            cur.execute("CREATE TABLE IF NOT EXISTS records (id SERIAL PRIMARY KEY, record_date DATE NOT NULL, member_name VARCHAR(50) REFERENCES members(name) ON DELETE CASCADE, project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE NULL, monthly_settlement_id INTEGER REFERENCES monthly_settlements(id) ON DELETE CASCADE NULL, cost_paid INTEGER NOT NULL, original_msg TEXT);")
            cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (COMPANY_NAME,))
        conn.commit()
    except Exception as e: conn.rollback(); app.logger.error(f"Init Error: {e}")
    finally: close_db_connection(conn)

init_db(force_recreate=False)

# --- 3. Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    original_text = event.message.text.strip()
    reply_token = event.reply_token
    response = None
    
    # V9.5: 全形轉半形 (含數字、運算符、逗號)
    trans_table = str.maketrans('０１２３４５６７８９，＋－＊／ｘＸ', '0123456789,+-*/xx')
    text_normalized = original_text.translate(trans_table)
    
    if text_normalized.lower() in ['幫助', '說明', '指令', 'help', 'menu', 'usage']:
        response = handle_help()
    else:
        mgmt_keywords = ('新增', '刪除', '清單', '統計', '結算', '報表', '測試', '強制重置', '設定固定點', '價目表')
        is_mgmt = text_normalized.startswith(mgmt_keywords)
        record_match = re.search(r'(\d{1,2}[/-]\d{1,2})', text_normalized)

        if is_mgmt or record_match:
            try:
                if is_mgmt:
                    cmd = text_normalized.split('\n')[0].strip()
                    if cmd.startswith('設定固定點') or cmd.startswith('新增'): response = handle_mgmt_add(cmd)
                    elif cmd.startswith('刪除'): response = handle_mgmt_delete(cmd)
                    elif cmd.startswith('清單'): response = handle_mgmt_list(cmd)
                    elif cmd.startswith('價目表'): response = handle_show_prices()
                    elif cmd.startswith('統計'): response = handle_mgmt_stat(cmd)
                    elif cmd.startswith('結算'): response = handle_settle_monthly(cmd)
                    elif cmd.startswith('報表'): response = handle_report(cmd)
                    elif cmd == '強制重置': init_db(force_recreate=True); response = "⚠️ 資料庫已重置 (V9.5.1)"
                    elif cmd == '測試': response = "✅ Bot V9.5.1 (Syntax Fixed) 運作正常"
                elif record_match:
                    response = handle_record_expense_smart(text_normalized)
            except Exception as e:
                app.logger.error(f"Error: {e}")
                response = f"❌ 錯誤: {e}"

    if response:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=response))

# --- 4. Core Logic ---

def handle_help():
    return """🤖 記帳機器人 V9.5.1

1️⃣ 日常記帳 (支援算式與倍數)
• 自動倍數：11/17(日) 彼 大慶 x2
• 算式模式：11/17(日) 彼 大慶 600+100
• 減法折扣：11/17(日) 彼 大慶 1200-50

2️⃣ 管理設定
• 新增人員 [人名1] [人名2]...
• 設定固定點 [地點] 月租 [元] 清潔 [元] 分攤 [人...] 營業日 [週...]
• 新增 [地點] [每次成本]

3️⃣ 財務
• 價目表 / 清單 / 統計 [人] [月]
• 報表 / 報表 11月
• 結算 月項目 [月] [地點+租金] [總額]"""

def calculate_days_in_month(year, month, open_days_str):
    if not open_days_str: return 0
    db_to_cal = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    clean_days = open_days_str.replace('，', ',')
    try:
        target_days = {db_to_cal[int(d)] for d in clean_days.split(',') if int(d) in db_to_cal}
    except: return 0
    count = 0
    matrix = calendar.monthcalendar(year, month)
    for week in matrix:
        for idx, day in enumerate(week):
            if day != 0 and idx in target_days: count += 1
    return count

def safe_eval(expr):
    """安全執行簡單數學運算"""
    try:
        if not re.match(r'^[\d\+\-\*\/\.\(\)]+$', expr): return None
        return int(eval(expr))
    except: return None

def handle_record_expense_smart(text):
    conn = get_db_connection()
    if not conn: return "❌ DB連線失敗"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT location_name FROM locations")
            all_locs = {row[0] for row in cur.fetchall()}
            
            date_match = re.search(r'(\d{1,2}[/-]\d{1,2})', text)
            if not date_match: return "❌ 日期錯誤"
            
            d_str = date_match.group(1).replace('-', '/')
            today = date.today()
            year = today.year + (1 if today.month==12 and d_str.startswith('1/') else (-1 if today.month==1 and d_str.startswith('12/') else 0))
            try: record_date = datetime.strptime(f'{year}/{d_str}', '%Y/%m/%d').date()
            except: return "❌ 日期無效"

            clean_text = text.replace(date_match.group(0), '')
            clean_text = re.sub(r'[\(\（].*?[\)\）]', '', clean_text)
            clean_text = re.sub(r'[桌布燈架]\d+.*', '', clean_text)
            clean_text = clean_text.replace('好', '').replace('ok', '').replace('OK', '')
            
            parts = clean_text.split()
            found_loc = None
            members = []
            
            # V9.5 算式邏輯
            manual_cost = None
            multiplier = 1

            for p in parts:
                p = p.strip()
                if not p: continue
                
                # 檢查倍數 (x2, *2)
                if re.match(r'^[x\*]\d+$', p.lower()):
                    multiplier = int(p.lower().replace('x', '').replace('*', ''))
                    continue

                # 檢查算式 (600+100)
                if re.search(r'[\+\-\*\/]', p) or p.isdigit():
                    val = safe_eval(p)
                    if val is not None:
                        manual_cost = val
                        continue
                
                is_loc = False
                for loc in all_locs:
                    if loc in p: found_loc = loc; is_loc = True; break
                if not is_loc and p != COMPANY_NAME: members.append(p)
            
            if not found_loc: return f"❌ 找不到地點。已知: {','.join(all_locs)}"
            if not members: return "❌ 未指定成員"

            for m in members:
                cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (m,))

            cur.execute("SELECT weekday_cost, linked_monthly_item FROM locations WHERE location_name=%s", (found_loc,))
            res = cur.fetchone()
            default_cost, link_item = res
            
            # 優先權：手動算式 > 倍數 > 預設
            if manual_cost is not None:
                final_cost = manual_cost
                note = "(手動金額)"
            elif multiplier > 1:
                final_cost = default_cost * multiplier
                note = f"(x{multiplier}倍)"
            else:
                final_cost = default_cost
                note = ""

            cur.execute("SELECT project_id, total_fixed_cost FROM projects WHERE record_date=%s AND location_name=%s", (record_date, found_loc))
            proj = cur.fetchone()
            is_std = '標準' in text; should_link = link_item and not is_std
            pid = None

            if not proj:
                cur.execute("INSERT INTO projects (record_date, location_name, total_fixed_cost, original_msg) VALUES (%s, %s, %s, %s) RETURNING project_id", (record_date, found_loc, final_cost, text))
                pid = cur.fetchone()[0]
                members_to_save = members
            else:
                pid, old_cost = proj
                if manual_cost is not None or multiplier > 1:
                    cur.execute("UPDATE projects SET total_fixed_cost = %s WHERE project_id = %s", (final_cost, pid))
                else:
                    final_cost = old_cost
                
                cur.execute("SELECT member_name FROM project_members WHERE project_id=%s", (pid,))
                existing = {r[0] for r in cur.fetchall()}
                members_to_save = list(existing.union(set(members)))
                cur.execute("DELETE FROM records WHERE project_id=%s", (pid,))
                cur.execute("DELETE FROM project_members WHERE project_id=%s", (pid,))

            for m in members_to_save:
                cur.execute("INSERT INTO project_members (project_id, member_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (pid, m))

            if members_to_save:
                half = final_cost // 2; per = half // len(members_to_save); comp = final_cost - (per * len(members_to_save))
            else: comp = final_cost; per = 0

            cur.execute("INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg) VALUES (%s, %s, %s, %s, %s)", (record_date, COMPANY_NAME, pid, comp, text))
            for m in members_to_save:
                cur.execute("INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg) VALUES (%s, %s, %s, %s, %s)", (record_date, m, pid, per, text))
            
            conn.commit()
            link_note = " (含月租抵扣)" if should_link else ""
            return f"✅ {found_loc} 紀錄完成\n📅 {record_date.strftime('%m/%d')}\n💰 {final_cost} {note}{link_note}\n🏢 公司: {comp}\n👤 夥伴({len(members_to_save)}人): 每人 {per}"
    except Exception as e: conn.rollback(); return f"❌ {e}"
    finally: close_db_connection(conn)

def handle_mgmt_add(text):
    parts = text.split()
    conn = get_db_connection()
    if not conn: return "❌ DB Error"
    try:
        with conn.cursor() as cur:
            # V9.4 新增人員
            if parts[0] == '新增人員':
                raw_args = text.replace('新增人員', '').replace(',', ' ')
                new_members = raw_args.split()
                added_list = []
                for m in new_members:
                    m = m.strip()
                    if m and m != COMPANY_NAME:
                        cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (m,))
                        added_list.append(m)
                conn.commit()
                if not added_list: return "❌ 請輸入人名"
                return f"✅ 已手動新增成員: {', '.join(added_list)}"

            # 計次通路
            if len(parts) == 3 and parts[0] == '新增' and parts[2].isdigit():
                try:
                    loc, cost = parts[1], int(parts[2])
                    cur.execute("""INSERT INTO locations (location_name, weekday_cost, linked_monthly_item, open_days) VALUES (%s, %s, NULL, NULL) ON CONFLICT (location_name) DO UPDATE SET weekday_cost=EXCLUDED.weekday_cost, linked_monthly_item=NULL;""", (loc, cost))
                    conn.commit()
                    return f"✅ 計次通路 {loc} 已儲存 (成本{cost})"
                except Exception as e: return f"❌ {e}"
            
            # 月租通路 (含全形逗號處理)
            if parts[0] == '設定固定點':
                try:
                    clean_text = text.replace('，', ',')
                    p_clean = clean_text.split()
                    if '月租' not in p_clean or '清潔' not in p_clean or '營業日' not in p_clean: return "❌ 格式錯誤"
                    loc = p_clean[1]
                    rent = int(p_clean[p_clean.index('月租')+1])
                    clean = int(p_clean[p_clean.index('清潔')+1])
                    days_str = p_clean[p_clean.index('營業日')+1]
                    share_idx = p_clean.index('分攤') if '分攤' in p_clean else -1
                    days_idx = p_clean.index('營業日')
                    members = []
                    if share_idx != -1:
                        raw_members = p_clean[share_idx+1 : days_idx]
                        for rm in raw_members:
                            for m in rm.split(','):
                                if m and m != COMPANY_NAME: members.append(m)
                    today = date.today()
                    day_map = {'日':'0','一':'1','二':'2','三':'3','四':'4','五':'5','六':'6'}
                    code_list = []
                    for d_char in days_str.split(','):
                        if d_char in day_map: code_list.append(day_map[d_char])
                    code = ','.join(code_list)
                    count = calculate_days_in_month(today.year, today.month, code)
                    if count == 0: return f"❌ 本月({today.month}月) 營業日計算為 0"
                    cost = int((rent / count) + clean)
                    item = f"{loc}租金"
                    cur.execute("INSERT INTO monthly_items (item_name, default_members, memo) VALUES (%s, %s, %s) ON CONFLICT (item_name) DO UPDATE SET default_members=EXCLUDED.default_members, memo=EXCLUDED.memo", (item, ','.join(members), f"月租{rent}"))
                    cur.execute("INSERT INTO locations (location_name, weekday_cost, linked_monthly_item, open_days) VALUES (%s, %s, %s, %s) ON CONFLICT (location_name) DO UPDATE SET weekday_cost=EXCLUDED.weekday_cost, linked_monthly_item=EXCLUDED.linked_monthly_item, open_days=EXCLUDED.open_days", (loc, cost, item, code))
                    for m in members: cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (m,))
                    conn.commit()
                    return f"✅ 月租通路 {loc} 設定完成 (單次{cost})\n(本月營業{count}天)"
                except Exception as e: return f"❌ 設定錯: {e}"

            if parts[0]=='新增人名':
                cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (parts[1],)); conn.commit(); return "OK"
            return "❌ 指令不明"
    finally: close_db_connection(conn)

def handle_mgmt_delete(text):
    parts = text.split()
    table = {'人名':'members','地點':'locations','月項目':'monthly_items'}.get(parts[1])
    col = {'人名':'name','地點':'location_name','月項目':'item_name'}.get(parts[1])
    if not table: return "❌ 類型錯誤"
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE {col}=%s", (parts[2],))
            conn.commit()
            return f"✅ 已刪除"
    finally: close_db_connection(conn)

def handle_show_prices():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT l.location_name, l.weekday_cost, l.linked_monthly_item, m.memo FROM locations l LEFT JOIN monthly_items m ON l.linked_monthly_item = m.item_name")
            rows = cur.fetchall()
            if not rows: return "💰 空"
            fixed = []; adhoc = []
            for r in rows:
                info = f"📍 {r[0]}: ${r[1]:,} /次"
                if r[2]: fixed.append(f"{info} (ℹ️ {r[3].replace('自動生成: ', '')})")
                else: adhoc.append(info)
            msg = "💰 價目表\n\n"
            if fixed: msg += "🅰️ 月租型:\n" + "\n".join(fixed) + "\n\n"
            if adhoc: msg += "🅱️ 計次型:\n" + "\n".join(adhoc)
            return msg.strip()
    finally: close_db_connection(conn)

def handle_mgmt_list(text):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            msg = "📋 清單:\n"
            cur.execute("SELECT name FROM members")
            rows = cur.fetchall()
            msg += f"👤: {','.join([r[0] for r in rows])}\n"
            
            cur.execute("SELECT location_name, linked_monthly_item FROM locations")
            locs = cur.fetchall()
            fixed = [r[0] for r in locs if r[1]]
            adhoc = [r[0] for r in locs if not r[1]]
            if fixed: msg += "🏢 月租: " + ', '.join(fixed) + "\n"
            if adhoc: msg += "⛺ 計次: " + ', '.join(adhoc)
            return msg
    finally: close_db_connection(conn)

def handle_mgmt_stat(text):
    parts = text.split()
    name = parts[1]
    month = int(parts[2].replace('月','')) if len(parts)>2 else date.today().month
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(cost_paid) FROM records WHERE member_name=%s AND date_part('month', record_date)=%s", (name, month))
            val = cur.fetchone()[0]
            return f"📊 {name} {month}月: {val or 0:,}"
    finally: close_db_connection(conn)

def handle_settle_monthly(text):
    parts = text.split()
    try:
        month = int(parts[2].replace('月',''))
        item = parts[3]
        total = int(parts[4])
    except: return "❌ 格式錯"
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT default_members FROM monthly_items WHERE item_name=%s", (item,))
            res = cur.fetchone()
            if not res: return "❌ 無此項目"
            mems = [m for m in res[0].split(',') if m!=COMPANY_NAME]
            
            year = date.today().year
            cur.execute("SELECT location_name, open_days FROM locations WHERE linked_monthly_item=%s", (item,))
            locs = cur.fetchall()
            
            tdays = 0
            lnames = []
            for l, days in locs:
                lnames.append(l)
                tdays += calculate_days_in_month(year, month, days)
            if tdays==0: return "❌ 營業日為0"
            
            udays = 0
            if lnames:
                cur.execute("SELECT COUNT(*) FROM projects WHERE location_name = ANY(%s) AND date_part('month', record_date)=%s", (lnames, month))
                udays = cur.fetchone()[0]
            
            deduct = round(udays * (total / tdays))
            remain = max(0, total - deduct)
            sharers = mems + [COMPANY_NAME]
            per = remain // len(sharers)
            comp = per + (remain % len(sharers))
            
            sdate = date(year, month, 1)
            cur.execute("DELETE FROM monthly_settlements WHERE settlement_date=%s AND item_name=%s", (sdate, item))
            cur.execute("INSERT INTO monthly_settlements (item_name, settlement_date, cost_amount, actual_members, total_capacity, original_msg) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", (item, sdate, remain, ','.join(mems), tdays, text))
            sid = cur.fetchone()[0]
            
            cur.execute("INSERT INTO records (record_date, member_name, monthly_settlement_id, cost_paid, original_msg) VALUES (%s, %s, %s, %s, %s)", (sdate, COMPANY_NAME, sid, comp, text))
            for m in mems:
                cur.execute("INSERT INTO records (record_date, member_name, monthly_settlement_id, cost_paid, original_msg) VALUES (%s, %s, %s, %s, %s)", (sdate, m, sid, per, text))
            
            conn.commit()
            return f"✅ {month}月 {item} 結算\n總額: {total}\n抵扣: {deduct} ({udays}天)\n剩餘: {remain} (每人{per})"
    finally: close_db_connection(conn)

def handle_report(text):
    tm = date.today().month
    tmem = None
    parts = text.split()
    args = parts[1:] if len(parts) > 0 and parts[0] == '報表' else parts
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM members")
            all_m = [r[0] for r in cur.fetchall()]
            for a in args:
                if '月' in a or (a.isdigit() and len(a)<=2):
                    try: tm = int(a.replace('月',''))
                    except: pass
                elif a in all_m: tmem = a
            
            q = "SELECT member_name, SUM(cost_paid) FROM records WHERE date_part('month', record_date)=%s"
            p = [tm]
            title = f"{tm}月"
            if tmem:
                q += " AND member_name=%s"
                p.append(tmem)
                title += f"/{tmem}"
            
            q += " GROUP BY member_name"
            cur.execute(q, tuple(p))
            rows = cur.fetchall()
            
            if not rows: return f"📉 {title}: 無資料"
            cc = 0
            mc = []
            for n, amt in rows:
                if n == COMPANY_NAME: cc = amt
                else: mc.append(f"👤 {n}: {amt:,}")
            
            total = cc + sum([r[1] for r in rows if r[0] != COMPANY_NAME])
            msg = f"📉 {title} 財務表\n━━━\n💰 總成本: {total:,}\n🏢 公司: {cc:,}\n━━━\n🔻 薪資應扣:\n" + ('\n'.join(mc) if mc else "(無)")
            return msg
    finally: close_db_connection(conn)

if __name__ == "__main__":
    app.run()