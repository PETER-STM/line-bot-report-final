# -*- coding: utf-8 -*-
import re
import os
import calendar
import json
from datetime import datetime, date, timedelta
from database import get_db_connection, close_db_connection
from utils import calculate_effective_days

COMPANY_NAME = os.getenv('COMPANY_NAME', '公司')

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

KNOWN_MEMBERS = {
    '小明', '明', '勳', '泰慶', '海豚', '浣熊', '蘋果', '伊森', '小花', '小瑀', '布', '狐狸', '邦妮',
    '千', '傑', '彼', '連', '花', '更', 'Min', 'Lily', '阿傑', '恩', '慈', 'P', 'E',
    '幽靈', 'ghost', '宏', '虫', '蟲', '芽', '易', '施恩澤', '宣儒', '烏爾'
}

def export_to_google_sheet(month, name, sheet_url):
    """將資料匯出至 Google Sheets (V20.20 終極金鑰洗滌版)"""
    if not GSPREAD_AVAILABLE: return "❌ 系統缺少 gspread 套件"
    creds_json = os.getenv('GOOGLE_CREDENTIALS')
    if not creds_json: return "❌ 尚未在 Railway 設定 GOOGLE_CREDENTIALS 環境變數"

    try:
        # 💡 [終極洗滌] 針對 Railway 可能出現的 1~4 個斜線進行全面清洗
        # 先把 JSON 本身的雙重轉義修好
        clean_json = creds_json.replace('\\\\n', '\\n')
        creds_dict = json.loads(clean_json, strict=False)
        
        if 'private_key' in creds_dict:
            pk = creds_dict['private_key']
            # 強力清洗：將任何組合的斜線+n 轉換為真正的換行符號
            pk = pk.replace('\\\\n', '\n').replace('\\n', '\n')
            # 再次確保沒有遺漏的字串形式斜線
            creds_dict['private_key'] = pk
            
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_url(sheet_url)
    except Exception as e: return f"❌ Google 認證或開啟表單失敗: {e}"

    target_sheet_name = f"{month}月支出表"
    try:
        worksheet = sh.worksheet(target_sheet_name)
        worksheet.batch_clear(["E6:G100"]) 
    except gspread.exceptions.WorksheetNotFound:
        try:
            template_ws = sh.worksheet("模板")
            worksheet = sh.duplicate_sheet(template_ws.id, new_sheet_name=target_sheet_name)
        except gspread.exceptions.WorksheetNotFound: return "❌ 找不到名為「模板」的工作表！"

    conn = get_db_connection()
    if not conn: return "❌ DB連線失敗"
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT p.location_name, r.record_date, r.cost_paid 
                           FROM records r JOIN projects p ON r.project_id=p.project_id 
                           WHERE date_part('month', r.record_date)=%s AND r.member_name=%s 
                           ORDER BY r.record_date""", (month, name))
            rows = cur.fetchall()
            if not rows: return f"⚠️ 找不到 {month}月 【{name}】 的記帳資料。"

            update_data = []
            weekdays = ["一", "二", "三", "四", "五", "六", "日"]
            for r in rows:
                loc, date_val, cost = r[0], r[1], r[2]
                date_str = f"{date_val.month}/{date_val.day} ({weekdays[date_val.weekday()]})"
                update_data.append([loc, date_str, cost])

            if update_data:
                last_day = calendar.monthrange(date.today().year, month)[1]
                worksheet.update_acell('C2', f"{month}/1~{month}/{last_day}")
                worksheet.update_acell('E2', name)
                cell_range = f"E6:G{5 + len(update_data)}"
                worksheet.update(range_name=cell_range, values=update_data)
                
            return f"✅ **匯出成功！**\n📂 已將 {len(update_data)} 筆資料寫入【{target_sheet_name}】。"
    except Exception as e: return f"❌ 寫入資料失敗: {e}"
    finally: close_db_connection(conn)

def handle_amend_last(text):
    conn = get_db_connection()
    if not conn: return "❌ DB連線失敗"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT p.project_id, p.location_name, p.record_date, p.total_fixed_cost, p.original_msg FROM projects p ORDER BY p.project_id DESC LIMIT 1")
            row = cur.fetchone()
            if not row: return "⚠️ 目前沒有任何紀錄可供修改"
            pid, loc, rec_date, old_cost, old_msg = row
            
            if text.startswith('改價') or text.startswith('改金額'):
                match = re.search(r'(\d+)', text)
                if not match: return "❌ 請輸入金額"
                new_cost = int(match.group(1))
                cur.execute("SELECT member_name FROM project_members WHERE project_id = %s", (pid,))
                members = [r[0] for r in cur.fetchall()]
                num_members = len(members)
                if num_members > 0:
                    per = (new_cost // 2) // num_members; comp = new_cost - (per * num_members)
                else: per = 0; comp = new_cost
                cur.execute("UPDATE projects SET total_fixed_cost = %s WHERE project_id = %s", (new_cost, pid))
                cur.execute("UPDATE records SET cost_paid = %s WHERE project_id = %s AND member_name = %s", (comp, pid, COMPANY_NAME))
                for m in members: cur.execute("UPDATE records SET cost_paid = %s WHERE project_id = %s AND member_name = %s", (per, pid, m))
                conn.commit()
                return f"💸 **金額已修正**\n📍 {loc} ({rec_date.strftime('%m/%d')})\n💰 新總額: {new_cost}\n🏢 公司: {comp}\n👤 夥伴: {per}"

            elif text.startswith('備註') or text.startswith('筆記'):
                note_content = text.replace('備註', '').replace('筆記', '').strip()
                if not note_content: return "❌ 請輸入備註內容"
                new_msg = f"{old_msg} | 📝備註: {note_content}"
                cur.execute("UPDATE projects SET original_msg = %s WHERE project_id = %s", (new_msg, pid))
                cur.execute("UPDATE records SET original_msg = %s WHERE project_id = %s", (new_msg, pid))
                conn.commit()
                return f"📝 **備註已追加**\n📍 {loc} ({rec_date.strftime('%m/%d')})\n💬 內容: {note_content}"
    except Exception as e: conn.rollback(); return f"❌ 修改失敗: {e}"
    finally: close_db_connection(conn)

def handle_admin(text):
    conn = get_db_connection()
    if not conn: return "❌ DB連線失敗"
    try:
        with conn.cursor() as cur:
            if text == '人員名單':
                cur.execute("SELECT name FROM members ORDER BY name")
                rows = cur.fetchall()
                if not rows: return "👥 目前無人員資料"
                return f"👥 **目前人員名單 ({len(rows)}人)**\n" + ", ".join([r[0] for r in rows])

            elif text.startswith('新增人員') or text.startswith('新增成員'):
                names_str = text.replace('新增人員', '').replace('新增成員', '')
                names = names_str.split(); added = []
                if not names: return "❌ 請輸入名字，例如：`新增人員 蛇蛇 連長`"
                for n in names: cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT DO NOTHING", (n,)); added.append(n)
                conn.commit(); return f"👤 已新增人員: {', '.join(added)}"

            elif text.startswith('新增'):
                parts = text.split()
                if len(parts) < 3: return "❌ 格式: `新增 [地點] [平日價] [假日價]`"
                loc = parts[1]
                wday = int(parts[2]); wend = int(parts[3]) if len(parts) > 3 else wday
                cur.execute("""INSERT INTO locations (location_name, weekday_cost, weekend_cost, category) 
                    VALUES (%s, %s, %s, '一般') ON CONFLICT (location_name) DO UPDATE SET weekday_cost=%s, weekend_cost=%s, category='一般'""", 
                    (loc, wday, wend, wday, wend))
                conn.commit()
                return f"✅ 地點 {loc} 設定完成\n☀️ 平日: ${wday}\n🌙 假日: ${wend}"

            elif text.startswith('設定別名'):
                parts = text.split(); alias, target = parts[1], parts[2]
                cur.execute("SELECT 1 FROM members WHERE name = %s", (target,))
                is_member = cur.fetchone()
                cur.execute("SELECT 1 FROM locations WHERE location_name = %s", (target,))
                is_location = cur.fetchone()
                if is_member:
                    cur.execute("INSERT INTO member_aliases (alias_name, target_name) VALUES (%s, %s) ON CONFLICT (alias_name) DO UPDATE SET target_name=%s", (alias, target, target))
                    conn.commit(); return f"👤 人員別名: {alias} -> {target}"
                elif is_location:
                    cur.execute("INSERT INTO location_aliases (alias_name, target_location) VALUES (%s, %s) ON CONFLICT (alias_name) DO UPDATE SET target_location=%s", (alias, target, target))
                    conn.commit(); return f"📍 地點別名: {alias} -> {target}"
                else: return f"⚠️ 找不到「{target}」"

# 👇 這裡插入我們的新器官 (必須在一般 '合併' 的上方，優先攔截！)
            elif text.startswith('合併地點'):
                parts = text.split()
                if len(parts) < 3: return "❌ 格式錯誤。請使用: `合併地點 [錯的地點] [對的地點]`"
                old_loc, new_loc = parts[1], parts[2]
                try:
                    cur.execute("SELECT 1 FROM locations WHERE location_name=%s", (new_loc,))
                    if not cur.fetchone():
                        return f"❌ 合併失敗：找不到目標地點「{new_loc}」。請先確保它已存在！"
                    
                    cur.execute("UPDATE projects SET location_name=%s WHERE location_name=%s", (new_loc, old_loc))
                    cur.execute("DELETE FROM locations WHERE location_name=%s", (old_loc,))
                    conn.commit()
                    return f"🔄 合併成功！已將「{old_loc}」的所有帳務移交給「{new_loc}」。"
                except Exception as e:
                    conn.rollback()
                    return f"❌ 合併發生錯誤: {e}"

            



            elif text.startswith('合併'):
                parts = text.split(); old_name, new_name = parts[1], parts[2]
                cur.execute("SELECT 1 FROM members WHERE name=%s", (old_name,))
                if not cur.fetchone(): return f"⚠️ 找不到「{old_name}」"
                cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (new_name,))
                cur.execute("UPDATE records SET member_name=%s WHERE member_name=%s", (new_name, old_name))
                rec_c = cur.rowcount
                cur.execute("SELECT project_id FROM project_members WHERE member_name=%s", (old_name,))
                for row in cur.fetchall(): cur.execute("INSERT INTO project_members (project_id, member_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (row[0], new_name))
                cur.execute("DELETE FROM project_members WHERE member_name=%s", (old_name,))
                cur.execute("DELETE FROM members WHERE name=%s", (old_name,))
                conn.commit(); return f"🔄 合併完成：{old_name} -> {new_name} (移轉 {rec_c} 筆)"


            elif text.startswith('清空月份'):
                parts = text.split(); target_param = parts[1]
                target_month = date.today().month; target_year = date.today().year
                m_match = re.search(r'(\d+)月', text)
                if m_match: 
                    target_month = int(m_match.group(1))
                    if date.today().month < target_month: target_year -= 1
                if target_param in ['全部', '所有', 'ALL', 'All']:
                    cur.execute("""DELETE FROM projects WHERE date_part('month', record_date) = %s AND date_part('year', record_date) = %s""", (target_month, target_year))
                    msg_loc = "🌍 所有地點"
                else:
                    cur.execute("""DELETE FROM projects WHERE location_name = %s AND date_part('month', record_date) = %s AND date_part('year', record_date) = %s""", (target_param, target_month, target_year))
                    msg_loc = f"📍 {target_param}"
                count = cur.rowcount; conn.commit()
                return f"🗑️ **已清空 {target_year}年{target_month}月 [{msg_loc}] 所有資料**\n共刪除 {count} 筆紀錄。"

            elif text.startswith('刪除'):
                parts = text.split()
                if len(parts) < 2: 
                    return "❌ 格式錯誤。請使用:\n1. `刪除 [日期] [地點]`\n2. `刪除 地點 [名稱]`\n3. `刪除 人員 [名稱]`"
                
                # 🛡️ 情境 1: 刪除特定日期的出攤紀錄 (例如: 刪除 4/18 旱溪)
                if '/' in parts[1]:
                    if len(parts) < 3: return "❌ 格式缺少地點，請輸入: `刪除 4/18 旱溪`"
                    date_str = parts[1]
                    loc_name = parts[2]
                    try:
                        month, day = map(int, date_str.split('/'))
                        target_date = date(date.today().year, month, day)
                        
                        cur.execute("SELECT project_id, original_msg FROM projects WHERE record_date=%s AND location_name LIKE %s", (target_date, f"%{loc_name}%"))
                        proj = cur.fetchone()
                        
                        if proj:
                            pid = proj[0]
                            orig_msg = proj[1]
                            
                            # [保險箱連線與防毒機制]
                            try:
                                cur.execute("INSERT INTO audit_logs (action_type, target_table, record_details) VALUES (%s, %s, %s)", 
                                    ('刪除出攤紀錄', 'projects', f"刪除 {date_str} {loc_name} | 原輸入: {orig_msg[:30]}"))
                                conn.commit() 
                            except Exception:
                                conn.rollback() 
                            
                            cur.execute("DELETE FROM projects WHERE project_id=%s", (pid,))
                            conn.commit()
                            return f"🗑️ **歷史清除完成**\n已成功刪除 {date_str} 【{loc_name}】 的所有出攤紀錄與相關帳務！"
                        else:
                            return f"⚠️ 找不到 {date_str} 【{loc_name}】 的出攤紀錄。"
                    except Exception as e:
                        conn.rollback() 
                        return f"❌ 刪除紀錄失敗: {e}"

              # 🛡️ 情境 2: 徹底刪除整個地點板塊 (數據資產阻斷保護版)
                elif parts[1] == '地點' and len(parts) >= 3:
                    loc_names = parts[2:]
                    try:
                        for n in loc_names:
                            # 💡 數據資產不可侵犯：先檢查該地點是否綁定任何歷史帳務
                            cur.execute("SELECT COUNT(*) FROM projects WHERE location_name=%s", (n,))
                            proj_count = cur.fetchone()[0]
                            
                            if proj_count > 0:
                                return f"❌ 拒絕刪除核彈：地點「{n}」底下綁定了 {proj_count} 筆歷史帳務！\n👉 若是錯帳，請先用 `刪除 [日期] {n}` 清除該筆帳務；若是歷史地點，請直接保留以維持報表完整。"
                            
                            # 確認為無關聯的「空殼地點」後，才允許刪除本體
                            cur.execute("DELETE FROM locations WHERE location_name=%s", (n,))
                        conn.commit()
                        return f"🗑️ 地點 [{', '.join(loc_names)}] 安全刪除完成"
                    except Exception as e:
                        conn.rollback()
                        return f"❌ 刪除地點失敗: {e}"

                # 🛡️ 情境 3: 徹底刪除幽靈人員 (補回遺失的人員刪除功能)
                elif parts[1] == '人員' and len(parts) >= 3:
                    names = parts[2:]
                    try:
                        for n in names: 
                            # 必須先刪除關聯的明細，才能刪除人員本體
                            cur.execute("DELETE FROM records WHERE member_name=%s", (n,))
                            cur.execute("DELETE FROM project_members WHERE member_name=%s", (n,))
                            cur.execute("DELETE FROM members WHERE name=%s", (n,))
                        conn.commit()
                        return f"🗑️ 人員 [{', '.join(names)}] 徹底刪除完成"
                    except Exception as e:
                        conn.rollback()
                        return f"❌ 刪除人員失敗: {e}"
                
                return "❌ 指令格式無效。\n👉 請輸入例如：`刪除 4/25 旱溪` 或 `刪除 地點 旱溪`"

            elif text.startswith('清除幽靈') or text.startswith('刪除幽靈'):
                parts = text.split(); target_loc = parts[1] if len(parts)>1 else ""
                if not target_loc: return "❌ 請指定地點"
                target_month = date.today().month
                m_match = re.search(r'(\d+)月', text)
                if m_match: target_month = int(m_match.group(1))
                cur.execute("""DELETE FROM projects WHERE location_name = %s AND date_part('month', record_date) = %s AND original_msg LIKE '自動補幽靈%%'""", (target_loc, target_month))
                conn.commit(); return f"🧹 已清除 **{target_loc} {target_month}月** 的自動補登紀錄。"
# 🛡️ [戰略移交術] 合併地點：將「錯的地圖」上的戰功，通通移交給「對的地圖」
            elif parts[1] == '合併地點' and len(parts) >= 4:
                    old_loc = parts[2]
                    new_loc = parts[3]
                    try:
                        # 1. 先確認「對的地圖」是否存在，不存在就拒絕執行，避免二次污染
                        cur.execute("SELECT 1 FROM locations WHERE location_name=%s", (new_loc,))
                        if not cur.fetchone():
                            return f"❌ 合併失敗：找不到目標地點「{new_loc}」。請先新增它！"
                        
                        # 2. 將所有記錯地點的「專案」與「紀錄」轉移到正確地點
                        cur.execute("UPDATE projects SET location_name=%s WHERE location_name=%s", (new_loc, old_loc))
                        cur.execute("UPDATE records SET project_id = p.project_id FROM projects p WHERE records.original_msg LIKE %s AND p.location_name=%s", (f"%{old_loc}%", new_loc))
                        
                        # 3. 戰功移交完成後，現在「舊地圖」是空的了，可以安全銷毀
                        cur.execute("DELETE FROM locations WHERE location_name=%s", (old_loc,))
                        
                        conn.commit()
                        return f"🔄 合併成功！已將「{old_loc}」的所有帳務移交給「{new_loc}」，並已移除錯誤地點。"
                    except Exception as e:
                        conn.rollback()
                        return f"❌ 合併發生錯誤: {e}"
# 🛡️ [戰略擴張術] 批量設定區段 (例如：設定區段 饒河 200~299 600)
            elif parts[0] == '設定區段' and len(parts) == 4:
                    prefix = parts[1] # 例如: 饒河
                    try:
                        start_num, end_num = map(int, parts[2].split('~'))
                        cost = int(parts[3])
                        
                        # 🚨 系統防禦網：防止手滑塞爆資料庫，限制一次最多佈署 500 個攤位
                        if end_num - start_num > 500 or end_num < start_num:
                            return "❌ 戰略範圍錯誤或過大！為保護資料庫，一次最多設定 500 個攤位。"
                            
                        # 啟動批量寫入與覆寫引擎 (Upsert)
                        for i in range(start_num, end_num + 1):
                            loc_name = f"{prefix}{i}"
                            cur.execute("""
                                INSERT INTO locations (location_name, weekday_cost, weekend_cost, category) 
                                VALUES (%s, %s, %s, '一般') 
                                ON CONFLICT (location_name) 
                                DO UPDATE SET weekday_cost=EXCLUDED.weekday_cost, weekend_cost=EXCLUDED.weekend_cost
                            """, (loc_name, cost, cost))
                        
                        conn.commit()
                        return f"✅ 戰略區段佈署完成！\n已將「{prefix}{start_num}」至「{prefix}{end_num}」共 {end_num - start_num + 1} 個攤位的預設成本，統一鎖定為 ${cost}。"
                    
                    except ValueError:
                        return "❌ 格式解析失敗！\n👉 正確範例：設定區段 饒河 200~299 600"
                    except Exception as e:
                        conn.rollback()
                        return f"❌ 系統發生錯誤: {e}"


            elif text.startswith('檢查缺漏'):
                parts = text.split(); target_loc = parts[1] if len(parts) > 1 else ""
                if not target_loc: return "❌ 請指定地點"
                target_month = date.today().month; target_year = date.today().year
                m_match = re.search(r'(\d+)月', text)
                if m_match: 
                    target_month = int(m_match.group(1))
                    if date.today().month < target_month: target_year -= 1
                cur.execute("SELECT business_days FROM locations WHERE location_name=%s", (target_loc,))
                row = cur.fetchone()
                if not row or not row[0]: return f"❌ {target_loc} 未設定營業日"
                week_map = {'一':0, '二':1, '三':2, '四':3, '五':4, '六':5, '日':6}
                target_days = [week_map[d] for d in row[0] if d in week_map]
                num_days = calendar.monthrange(target_year, target_month)[1]
                should_have_dates = []
                for d in range(1, num_days + 1):
                    cd = date(target_year, target_month, d)
                    if cd.weekday() in target_days: should_have_dates.append(cd)
                cur.execute("SELECT DISTINCT record_date FROM projects WHERE location_name=%s AND date_part('month', record_date)=%s", (target_loc, target_month))
                existing = {r[0] for r in cur.fetchall()}
                missing = [d for d in should_have_dates if d not in existing]
                if not missing: return f"✅ {target_loc} {target_month}月 無缺漏"
                msg = f"🔍 **{target_loc} {target_month}月 缺漏清單 ({len(missing)}天):**\n" + ", ".join([f"{d.day}號" for d in missing])
                return msg + f"\n\n💡 若要補登，請輸入：\n`一鍵補幽靈 {target_loc} {target_month}月`"

            elif text.startswith('一鍵補幽靈'):
                parts = text.split(); target_loc = parts[1] if len(parts) > 1 else ""
                if not target_loc: return "❌ 請指定地點"
                target_month = date.today().month; target_year = date.today().year
                m_match = re.search(r'(\d+)月', text)
                if m_match: 
                    target_month = int(m_match.group(1))
                    if date.today().month < target_month: target_year -= 1
                force_cost = None; cost_match = re.search(r'\s(\d+)$', text)
                if cost_match and int(cost_match.group(1)) > 12: force_cost = int(cost_match.group(1))
                cur.execute("SELECT monthly_rent, business_days, shared_members, cleaning_fee, weekday_cost FROM locations WHERE location_name=%s", (target_loc,))
                row = cur.fetchone()
                if not row: return "❌ 找不到地點設定"
                rent, days_str, share_str, cleaning, wday_cost = row
                share_mems = [m.strip() for m in share_str.split(',') if m.strip()]
                daily = 0; note = ""
                if force_cost: daily = force_cost; note = "(指定)"
                elif rent > 0:
                    eff_days = calculate_effective_days(target_year, target_month, days_str)
                    if eff_days == 0: eff_days = 30
                    daily = round(rent / eff_days) # 幽靈天只付房租
                    note = f"({rent}/{eff_days}天)"
                else:
                    daily = wday_cost; note = "(固定價)"
                week_map = {'一':0, '二':1, '三':2, '四':3, '五':4, '六':5, '日':6}
                target_days = [week_map[d] for d in days_str if d in week_map]
                cur.execute("SELECT DISTINCT record_date FROM projects WHERE location_name=%s AND date_part('month', record_date)=%s", (target_loc, target_month))
                existing = {r[0] for r in cur.fetchall()}
                count = 0
                num_days_in_month = calendar.monthrange(target_year, target_month)[1]
                for d in range(1, num_days_in_month + 1):
                    cd = date(target_year, target_month, d)
                    if cd.weekday() in target_days and cd not in existing:
                        orig = f"自動補幽靈 {cd.strftime('%m/%d')}"
                        cur.execute("INSERT INTO projects (record_date, location_name, total_fixed_cost, original_msg) VALUES (%s, %s, %s, %s) RETURNING project_id", (cd, target_loc, daily, orig))
                        pid = cur.fetchone()[0]
                        per = (daily // 2) // len(share_mems); comp = daily - (per * len(share_mems))
                        
                        cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (COMPANY_NAME,))

                        cur.execute("INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg) VALUES (%s, %s, %s, %s, %s)", (cd, COMPANY_NAME, pid, comp, orig))
                        for m in share_mems:
                            cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT DO NOTHING", (m,))
                            cur.execute("INSERT INTO project_members (project_id, member_name) VALUES (%s, %s)", (pid, m))
                            cur.execute("INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg) VALUES (%s, %s, %s, %s, %s)", (cd, m, pid, per, orig))
                        count += 1
                conn.commit()
                return f"👻 **補登完成**\n地點: {target_loc} ({target_month}月)\n補登天數: {count} 天\n單日金額: {daily} {note}\n分攤人: {', '.join(share_mems)}"
           
            elif text.startswith('設定固定點'):
                try:
                    parts = text.split(); loc = parts[1]
                    rent = int(re.search(r'月租\s*(\d+)', text).group(1) or 0)
                    cleaning = int(re.search(r'清潔\s*(\d+)', text).group(1) or 0) 
                    days = re.search(r'營業日\s*(.*)', text).group(1).strip()
                    share_match = re.search(r'分攤\s*(.*?)(?=\s+營業日|$)', text)
                    share_list_str = share_match.group(1).strip().replace(' ', ',') if share_match else ""
                    if not share_list_str: return "❌ 未設定分攤人員"
                    today = date.today()
                    effective_days = calculate_effective_days(today.year, today.month, days)
                    preview_cost = round(rent / effective_days) + cleaning if effective_days else 0
                    cur.execute("""INSERT INTO locations (location_name, weekday_cost, weekend_cost, category, monthly_rent, cleaning_fee, business_days, shared_members) 
                        VALUES (%s, %s, %s, '月租', %s, %s, %s, %s) ON CONFLICT (location_name) 
                        DO UPDATE SET monthly_rent=%s, cleaning_fee=%s, category='月租', business_days=%s, shared_members=%s, weekday_cost=%s, weekend_cost=%s""", 
                        (loc, preview_cost, preview_cost, rent, cleaning, days, share_list_str, rent, cleaning, days, share_list_str, preview_cost, preview_cost))
                    conn.commit()
                    return f"✅ **固定點 [{loc}] 設定完成**\n💰 月租: {rent} / 清潔: {cleaning}(日)\n📅 營業日: {days}\n👥 分攤: {share_list_str}\n🔢 本月出攤成本: {preview_cost} (含清潔)\n👻 本月幽靈成本: {round(rent/effective_days)} (免清潔)"
                except Exception as e: return f"❌ 設定失敗: {e}"

            # 💡 把它放在大門裡面，接在設定固定點的後面！
            elif text.startswith('設定百貨'):
                parts = text.split()
                if len(parts) < 2: return "❌ 格式: `設定百貨 [百貨名稱]` (例如: 設定百貨 宏匯)"
                loc = parts[1]
                cur.execute("""INSERT INTO locations (location_name, weekday_cost, weekend_cost, category) 
                    VALUES (%s, 0, 0, '百貨') ON CONFLICT (location_name) DO UPDATE SET weekday_cost=0, weekend_cost=0, category='百貨'""", 
                    (loc,))
                conn.commit()
                return f"🏬 **百貨通路 [{loc}] 設定完成**\n✅ 預設出攤成本為 $0，支援動態抽成與零元結算！"

    # 💡 這裡是最後的關門動作
    except Exception as e: 
        conn.rollback()
        return f"❌ 管理錯誤: {e}"
    finally: 
        close_db_connection(conn)


def handle_finance(text):
    """財務查詢與報表匯出 (V20.20 智能網址吸塵器)"""
    if text.startswith('匯出'):
        try:
            http_idx = text.find('http')
            if http_idx == -1: return "❌ 找不到網址 (請確保有包含 http)"
            
            raw_url = text[http_idx:]
            clean_url = re.sub(r'\s+', '', raw_url) 
            
            front_part = text[:http_idx].strip()
            
            m_match = re.search(r'(\d+)月', front_part)
            if not m_match: return "❌ 找不到月份，請輸入如 `2月`"
            month = int(m_match.group(1))
            
            name_str = front_part.replace('匯出', '').replace(m_match.group(0), '').strip()
            if not name_str: return "❌ 找不到姓名，請確認格式：`匯出 2月 彼得 [網址]`"
            name = name_str.split()[0]
            
            return export_to_google_sheet(month, name, clean_url)
        except Exception as e: return f"❌ 匯出指令解析失敗: {e}"

    conn = get_db_connection()
    if not conn: return "❌ DB連線失敗"
    try:
        with conn.cursor() as cur:
            if text in ['價目表', '清單', '統計']:
                cur.execute("SELECT location_name, weekday_cost, weekend_cost, category, monthly_rent FROM locations ORDER BY category, location_name")
                rows = cur.fetchall(); msg = "💰 **價目表**\n"
                for r in rows:
                    if r[3] == '月租': msg += f"📍 {r[0]}: 月租計費 (租金${r[4]})\n"
                    else:
                        price = f"${r[1]}" if r[1] == r[2] else f"平${r[1]} / 假${r[2]}"
                        msg += f"📍 {r[0]}: {price}\n"
                return msg

            elif text.startswith('檔期結算'):
                try:
                    parts = text.split()
                    date_range = parts[1]
                    loc_keyword = parts[2]
                    total_cost = int(parts[3])

                    start_str, end_str = date_range.split('~')
                    y = date.today().year
                    start_date = datetime.strptime(f"{y}/{start_str}", "%Y/%m/%d").date()
                    end_date = datetime.strptime(f"{y}/{end_str}", "%Y/%m/%d").date()

                    cur.execute("SELECT project_id FROM projects WHERE record_date >= %s AND record_date <= %s AND location_name LIKE %s", (start_date, end_date, f"%{loc_keyword}%"))
                    pids = [r[0] for r in cur.fetchall()]
                    if not pids: return f"⚠️ 找不到 {date_range} 期間【{loc_keyword}】的紀錄"

                    cur.execute("""
                        SELECT m.name, COUNT(*) FROM records r 
                        JOIN projects p ON r.project_id = p.project_id 
                        JOIN members m ON r.member_name = m.name 
                        WHERE r.record_date >= %s AND r.record_date <= %s AND p.location_name LIKE %s AND r.member_name != %s 
                        GROUP BY m.name
                    """, (start_date, end_date, f"%{loc_keyword}%", COMPANY_NAME))
                    rows = cur.fetchall()
                    total_days = sum(r[1] for r in rows)
                    
                    if total_days == 0: return "⚠️ 該檔期內無夥伴紀錄"

                    per_person_day = (total_cost // 2) // total_days
                    daily_total_approx = total_cost // len(pids)

                    for pid in pids:
                        cur.execute("SELECT COUNT(*) FROM project_members WHERE project_id=%s", (pid,))
                        m_count = cur.fetchone()[0]
                        comp_pay = daily_total_approx - (per_person_day * m_count)
                        
                        cur.execute("UPDATE projects SET total_fixed_cost=%s WHERE project_id=%s", (daily_total_approx, pid))
                        cur.execute("UPDATE records SET cost_paid=%s WHERE project_id=%s AND member_name!=%s", (per_person_day, pid, COMPANY_NAME))
                        cur.execute("UPDATE records SET cost_paid=%s WHERE project_id=%s AND member_name=%s", (comp_pay, pid, COMPANY_NAME))
                    
                    conn.commit()

                    msg = f"🎪 **檔期結算寫入完成: {loc_keyword}**\n📅 期間: {date_range}\n💰 總額更新為: {total_cost} (共 {total_days} 人次)\n"
                    msg += "-"*15 + "\n"
                    for r in rows:
                        member_cost = r[1] * per_person_day
                        msg += f"👤 {r[0]}: {r[1]}天 = 應付 ${member_cost}\n"
                    msg += "-"*15 + "\n💡 (資料庫已全面更新，如成本有變，直接重新輸入指令即可覆蓋)"
                    return msg
                except Exception as e:
                    conn.rollback()
                    return f"❌ 格式錯誤！請輸入如：`檔期結算 1/28~2/5 A攤 10000` (錯誤細節: {e})"

            elif text.startswith('結算'):
                m_match = re.search(r'(\d+)月', text); cost_match = re.search(r'(\d+)$', text.strip())
                if m_match and cost_match:
                    month = int(m_match.group(1)); total_cost = int(cost_match.group(1))
                    clean_key = re.sub(r'(結算|月|\d+)', '', text).strip()
                    cur.execute("""SELECT m.name, COUNT(*) FROM records r JOIN projects p ON r.project_id=p.project_id JOIN members m ON r.member_name=m.name 
                        WHERE date_part('month', r.record_date)=%s AND p.location_name LIKE %s AND r.member_name!=%s GROUP BY m.name""", (month, f"%{clean_key[:2]}%", COMPANY_NAME))
                    rows = cur.fetchall(); total_days = sum(r[1] for r in rows)
                    if not total_days: return "⚠️ 無資料"
                    per = total_cost // total_days
                    msg = f"💰 {month}月 {clean_key} 結算 (總額 {total_cost}, 每人 {per})\n"
                    for r in rows: msg += f"{r[0]}: {r[1]}天 = {r[1]*per}\n"
                    return msg
                return "❌ 結算格式: `結算 2月彼得 8000`"

            elif text.startswith('百貨'):
                # 擷取地點與月份 (例如: 百貨 宏匯 4月)
                parts = text.split()
                if len(parts) < 2: return "❌ 格式: `百貨 [地點] [月份]` (例如: 百貨 宏匯 4月)"
                loc_keyword = parts[1]
                
                target_month = date.today().month
                m_match = re.search(r'(\d+)月', text)
                if m_match: target_month = int(m_match.group(1))

                cur.execute("""
                    SELECT p.record_date, r.member_name 
                    FROM records r 
                    JOIN projects p ON r.project_id=p.project_id 
                    WHERE p.location_name LIKE %s AND date_part('month', p.record_date)=%s AND r.member_name != %s 
                    ORDER BY p.record_date, r.member_name
                """, (f"%{loc_keyword}%", target_month, COMPANY_NAME))
                
                rows = cur.fetchall()
                if not rows: return f"⚠️ 找不到 {target_month}月 【{loc_keyword}】 的排班紀錄"
                
                # 將同一天的人員組合起來
                from collections import defaultdict
                date_members = defaultdict(list)
                for r in rows:
                    date_members[r[0]].append(r[1])
                    
                msg = f"🏬 **{target_month}月 {loc_keyword} 出勤統計**\n" + "-"*15 + "\n"
                total_shifts = 0
                for d, mems in date_members.items():
                    msg += f"📅 {d.strftime('%m/%d')}: {', '.join(mems)}\n"
                    total_shifts += len(mems)
                    
                msg += "-"*15 + f"\n總計: 共 {len(date_members)} 天 / {total_shifts} 人次"
                return msg

            elif '明細' in text or '完整' in text:
                tm = int(re.search(r'(\d+)月', text).group(1)) if re.search(r'(\d+)月', text) else date.today().month
                cur.execute("""SELECT r.record_date, p.location_name, r.member_name, r.cost_paid, p.original_msg 
                    FROM records r JOIN projects p ON r.project_id=p.project_id WHERE date_part('month', r.record_date)=%s ORDER BY r.record_date DESC""", (tm,))
                rows = cur.fetchall()
                if not rows: return f"📋 {tm}月無資料"
                msg = f"📋 **{tm}月明細**\n"
                for r in rows:
                    cost_str = f"${r[3]}" if r[3] >= 0 else "⚠️待核算"
                    msg += f"{r[0].strftime('%m/%d')} {r[1]} | {r[2]} {cost_str} | 📝{r[4][:10]}\n"
                return msg

            else: 
                tm = int(re.search(r'(\d+)月', text).group(1)) if re.search(r'(\d+)月', text) else date.today().month
                tmem = None
                cur.execute("SELECT name FROM members"); all_m = [r[0] for r in cur.fetchall()]
                for p in text.split():
                    if p in all_m: tmem = p; break
                
                if tmem:
                    cur.execute("""SELECT r.record_date, p.location_name, r.cost_paid FROM records r JOIN projects p ON r.project_id=p.project_id 
                        WHERE date_part('month', r.record_date)=%s AND r.member_name=%s ORDER BY r.record_date""", (tm, tmem))
                    rows = cur.fetchall()
                    total = sum(r[2] for r in rows)
                    msg = f"📊 **{tm}月報表 ({tmem})**\n" + "-"*15 + "\n"
                    for r in rows:
                        cost_str = f"${r[2]}" if r[2] >= 0 else "⚠️待核算"
                        msg += f"{r[0].strftime('%m/%d')} {r[1]}: {cost_str}\n"
                    return msg + "-"*15 + f"\n**總計: ${total}**"
                else:
                    cur.execute("""SELECT member_name, SUM(cost_paid) FROM records WHERE date_part('month', record_date)=%s GROUP BY member_name ORDER BY SUM(cost_paid) DESC""", (tm,))
                    rows = cur.fetchall()
                    msg = f"📊 **{tm}月總報表**\n" + "-"*15 + "\n"
                    for r in rows: msg += f"{r[0]}: ${r[1]}\n"
                    return msg + "-"*15 + f"\n總計: ${sum(r[1] for r in rows)}"

    except Exception as e: return f"❌ 財務錯誤: {e}"
    finally: close_db_connection(conn)

def handle_help_visual():
    return """🤖 **Ahab2.0 旗艦版指令大全** 🤖

📝 **【日常記帳與修改】**
👉 `4/2 蟲 勝利夜市 500` (新地點自動學習)
👉 `改價 450` (修改剛記完的上一筆金額)
👉 `備註 提早收攤` (幫上一筆加註記)

👥 **【人員與別名防呆】**
👉 `新增人員 BOSS 蛇蛇` (建檔新夥伴)
👉 `設定別名 阿 阿傑` (教機器人阿=阿傑)
👉 `合併 ily Lily` (將舊帳與名字轉移)
👉 `人員名單` (查看所有已建檔夥伴)

🏢 **【地點與固定攤設定】**
👉 `新增 饒河219 500 600` (設平/假日價)
👉 `設定固定點 三和 月租 10000 清潔 50 營業日 一二三四 分攤 小明,阿傑`
👉 `價目表` (查看所有地點設定)

📊 **【報表與進階結算】**
👉 `3月報表` / `3月報表 Lily`
👉 `3月明細` (查看每日逐筆紀錄)
👉 `檔期結算 1/28~2/5 A攤 10000` (按天平分)
👉 `匯出 3月 Lily [Google表單網址]`

🧹 **【大掃除與防護】**
👉 `刪除 4/2 夜市` (刪除單筆錯帳)
👉 `刪除 人員 夜市` (抹除資料庫假人)
👉 `刪除 地點 總站` (批量刪除廢棄地點)
👉 `檢查缺漏 三和 3月` (查固定攤誰沒點名)
👉 `一鍵補幽靈 三和 3月` (自動補齊扣款)
👉 `清除異常` (清理系統亂碼)"""