# -*- coding: utf-8 -*-
import re
import os
from datetime import datetime, date
from database import get_db_connection, close_db_connection
from utils import safe_eval, calculate_effective_days, clean_input_text, smart_split_text

try:
    from rapidfuzz import process, fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

def fuzzy_match_entity(target_word, choices_dict, threshold=80):
    """智慧模糊比對引擎 (NLP 級別的錯字修復)"""
    if not RAPIDFUZZ_AVAILABLE or not target_word or not choices_dict:
        return None

    candidates = list(choices_dict.keys())
    match_result = process.extractOne(target_word, candidates, scorer=fuzz.WRatio)
    
    if match_result:
        best_match, score, _ = match_result
        if score >= threshold:
            return choices_dict[best_match]
            
    return None

COMPANY_NAME = os.getenv('COMPANY_NAME', '公司')

KNOWN_MEMBERS = {
    '小明', '明', '勳', '泰慶', '海豚', '浣熊', '蘋果', '伊森', '小花', '小瑀', '布', '狐狸', '邦妮',
    '千', '傑', '彼', '連', '花', '更', 'Min', 'Lily', '阿傑', '恩', '慈', 'P', 'E',
    '幽靈', 'ghost', '宏', '虫', '蟲', '芽', '易', '施恩澤', '宣儒', '烏爾'
}

def ensure_location_exists_strict(conn, location_name, base_loc=None):
    """嚴格確保地點存在"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM locations WHERE location_name=%s", (location_name,))
            if cur.fetchone(): return True

            if base_loc:
                try:
                    cur.execute("""INSERT INTO locations 
                        (location_name, weekday_cost, weekend_cost, surcharge, category, monthly_rent, cleaning_fee, business_days, shared_members)
                        SELECT %s, weekday_cost, weekend_cost, surcharge, category, monthly_rent, cleaning_fee, business_days, shared_members
                        FROM locations WHERE location_name=%s
                        ON CONFLICT DO NOTHING""", (location_name, base_loc))
                except: pass 
            
            cur.execute("SELECT 1 FROM locations WHERE location_name=%s", (location_name,))
            if not cur.fetchone():
                cur.execute("""INSERT INTO locations (location_name, weekday_cost, weekend_cost, category)
                    VALUES (%s, 400, 400, '一般') ON CONFLICT DO NOTHING""", (location_name,))
            conn.commit()
            return True
    except Exception as e: 
                conn.rollback()
                return False

def handle_record_expense_smart(text):
    text = clean_input_text(text)
    if len(text.split('\n')) > 3 or len(text) > 100: return None

    conn = get_db_connection()
    if not conn: return "❌ DB連線失敗"
    try:
        with conn.cursor() as cur:
            is_newly_learned = False

            # 1. 準備資料
            cur.execute("SELECT location_name, monthly_rent, cleaning_fee, weekday_cost, business_days, shared_members FROM locations")
            loc_data = {row[0]: {'rent': row[1], 'clean': row[2], 'cost': row[3], 'days': row[4], 'shared': row[5]} for row in cur.fetchall()}
            
            cur.execute("SELECT alias_name, target_location FROM location_aliases")
            alias_map = {row[0]: row[1] for row in cur.fetchall()}
            
            cur.execute("SELECT alias_name, target_name FROM member_aliases")
            mem_alias_map = {row[0]: row[1] for row in cur.fetchall()}

            all_locs = list(loc_data.keys())
            cur.execute("SELECT name FROM members")
            db_members = {row[0] for row in cur.fetchall()}
            dynamic_known_members = KNOWN_MEMBERS.union(db_members).union(set(mem_alias_map.keys()))

            # 2. 日期與廢字處理
            date_match = re.search(r'(\d{1,2}[/-]\d{1,2})', text)
            if not date_match: return None
            d_str = date_match.group(1).replace('-', '/')
            today = date.today()
            year = today.year + (1 if today.month==12 and d_str.startswith('1/') else (-1 if today.month==1 and d_str.startswith('12/') else 0))
            
            try:
                record_date = datetime.strptime(f'{year}/{d_str}', '%Y/%m/%d').date()
            except Exception:
                return None

            # 1. 預處理
            text_processed = text.replace('（', '(').replace('）', ')').replace('，', ' ').replace('、', ' ')
            
            # 👇 物理消滅鍵盤隱形符號 (Emoji Variation Selector)
            text_processed = text_processed.replace('\ufe0f', '')
            # 👇 物理消滅時間標記、干擾符號與【日期本體】
            text_processed = re.sub(r'[\(（][一二三四五六日][\)）]', ' ', text_processed)
            text_processed = re.sub(r'[\(（]\d+[\)）]', ' ', text_processed)
            text_processed = re.sub(r'\d{1,2}[:：/；;]\d{2}', ' ', text_processed)
            text_processed = text_processed.replace('+', ' ')
            
            # 🚨 關鍵防禦：用正規表示式徹底把日期 (例如 5/1, 4/25) 從文字中抹

            # 🚨 1. 精準抹除真正的日期 (把最上方捕捉到的 5/1 完美消滅，絕不傷及無辜數學式)
            if date_match:
                text_processed = text_processed.replace(date_match.group(1), ' ', 1)
                
            # 💡 [數字脫鉤術] 強制將緊黏著數字的括號與中文字推開 (如 950(角位) ➔ 950 (角位) / 950角位 ➔ 950 角位)
            text_processed = re.sub(r'(\d+)([\(（])', r'\1 \2', text_processed)
            text_processed = re.sub(r'([\)）])(\d+)', r'\1 \2', text_processed)
            text_processed = re.sub(r'(\d{2,})([\u4e00-\u9fa5]+)', r'\1 \2', text_processed)
                
            # 🚨 2. [終極全鍵盤防禦網] 撲殺所有手滑打錯的「時間刺客」
            # 黑名單：冒號、分號、斜線、反斜線、小數點、句號、底線、直線、單雙引號 (全半形)
            # 白名單：排除逗號(保護金額 1,500)、排除加減乘(保護數學引擎 safe_eval)
            text_processed = re.sub(r'\d{1,2}[:：/／；;\.．。_＿\\｜\|"＂\'＇]\d{2}', ' ', text_processed) 

            # 💡 [優先執行] 擴充版停用詞黑名單
            stopwords = ['更', '好', 'ok', 'OK', '已下攤', '圖片', '陌開', '是', '唷', '喔', '啊', '啦', '嗯', '的', '了', '呢', '補']
            for word in stopwords: 
                text_processed = text_processed.replace(word, ' ')

            # 💡 [延後執行] 智能切割刀
            text_processed = re.sub(r'([\u4e00-\u9fa5a-zA-Z]+)(0|[1-9]\d*0)(?=\s|$)', r'\1 \2', text_processed)

            # 3. 💡 [核心修復] 價格萃取 (從右到左，避免吃到攤位號碼)
            temp_scan_text = smart_split_text(text_processed)
            temp_scan_parts = re.split(r'[,\s、，]+', temp_scan_text)
            manual_override = None
            
            for p in reversed(temp_scan_parts):
                p_clean = p.strip().replace(',', '').replace('元', '').replace('$', '')
                if p_clean.isdigit():
                    if len(p_clean) == 8 and p_clean.startswith('20'): continue
                    manual_override = int(p_clean)
                    # 從字串中安全移除該價格，避免後續地點解析抓錯
                    parts = text_processed.rsplit(p, 1)
                    text_processed = " ".join(parts)
                    break

            # 4. 地點掃描 
            found_loc = None
            sorted_locs = sorted(all_locs, key=len, reverse=True)
            
            # (4-1) 黏字掃描 (例如：小花饒河)
            temp_text_for_loc = smart_split_text(text_processed)
            raw_parts_for_loc = re.split(r'[,\s、，]+', temp_text_for_loc)
            for p in raw_parts_for_loc:
                p_clean = p.strip()
                if not p_clean or p_clean.isdigit(): continue
                for loc in sorted_locs:
                    if p_clean.endswith(loc) and len(p_clean) > len(loc):
                        prefix = p_clean[:-len(loc)]
                        if not prefix.isdigit() and prefix not in dynamic_known_members:
                            found_loc = p_clean
                            ensure_location_exists_strict(conn, found_loc, base_loc=loc)
                            text_processed = text_processed.replace(p_clean, " ")
                            break
                if found_loc: break

            # (4-2) 💡 [核心修復] 變體掃描 (允許 饒河 156, 饒河 156號，不再受限於數字大小)
            if not found_loc:
                for loc in sorted_locs:
                    # 匹配：地點 + 可選空白 + 英數字 + 可選空白 + '號'
                    variant_match = re.search(f"({re.escape(loc)}\\s*[A-Za-z0-9]+\\s*號?)", text_processed)
                    if variant_match:
                        raw_found = variant_match.group(1)
                        # 💡 商業邏輯校準：恢復實體獨立性。因為攤位價格不同，必須各自擁有獨立的資料庫地基。
                        found_loc = raw_found.replace(" ", "").replace("號", "")
                        
                        if found_loc not in all_locs:
                            return f"⚠️ 找不到「{found_loc}」這個地點，我先沒記。\n▸ 新通路請先建檔：新增{found_loc} [金額]\n▸ 打錯字請用正確地點名重打一次"
                             
                        text_processed = text_processed.replace(raw_found, " ")
                        break

           # (4-3) 模糊比對與別名掃描 (含自我修復網)
            healing_note = ""
            if not found_loc:
                # 建立比對字典 { "候選字" : "標準地點" }
                loc_choices = {}
                for alias, target in alias_map.items(): loc_choices[alias] = target
                for loc in all_locs: loc_choices[loc] = loc
                
                # (4-3) 模糊比對與別名掃描 (含自我修復網)
            healing_note = ""
            if not found_loc:
                # 建立比對字典 { "候選字" : "標準地點" }
                loc_choices = {}
                for alias, target in alias_map.items(): loc_choices[alias] = target
                for loc in all_locs: loc_choices[loc] = loc
                
                # 1. 先嘗試傳統的精準匹配 (保留高效能)
                search_list = sorted(loc_choices.items(), key=lambda x: len(x[0]), reverse=True)
                for search_term, target_loc in search_list:
                    if search_term in text_processed:
                        found_loc = target_loc
                        text_processed = text_processed.replace(search_term, " ", 1)
                        if search_term != target_loc:
                            healing_note = f" (🤖修復: {search_term}➔{target_loc})"
                            try:
                                cur.execute("INSERT INTO error_logs (error_type, error_message, original_input) VALUES (%s, %s, %s)", 
                                    ('自我修復', f'將 [{search_term}] 自動修正為 [{target_loc}]', text))
                            except Exception:
                                conn.rollback()
                        break
                
                # 2. 💡 [神經網路重啟] 啟動 RapidFuzz 模糊比對空中攔截！
                if not found_loc and RAPIDFUZZ_AVAILABLE:
                    temp_parts = [p for p in re.split(r'[,\s、，]+', text_processed) if p.strip() and not p.isdigit()]
                    for p in temp_parts:
                        fuzzy_result = fuzzy_match_entity(p, loc_choices, threshold=82) # 門檻設 82 避免誤判
                        if fuzzy_result:
                            found_loc = fuzzy_result
                            text_processed = text_processed.replace(p, " ", 1)
                            healing_note = f" (🤖模糊修復: {p}➔{found_loc})"
                            try:
                                cur.execute("INSERT INTO error_logs (error_type, error_message, original_input) VALUES (%s, %s, %s)", 
                                    ('模糊修復', f'相似度匹配將 [{p}] 修正為 [{found_loc}]', text))
                            except Exception:
                                conn.rollback()
                            break
                
                # 2. 啟動 RapidFuzz 模糊比對空中攔截！
                if not found_loc and RAPIDFUZZ_AVAILABLE:
                    temp_parts = [p for p in re.split(r'[,\s、，]+', text_processed) if p.strip() and not p.isdigit()]
                    for p in temp_parts:
                        fuzzy_result = fuzzy_match_entity(p, loc_choices, threshold=82) # 門檻設 82 避免誤判
                        if fuzzy_result:
                            found_loc = fuzzy_result
                            text_processed = text_processed.replace(p, " ", 1)
                            healing_note = f" (🤖模糊修復: {p}➔{found_loc})"
                            try:
                                cur.execute("INSERT INTO error_logs (error_type, error_message, original_input) VALUES (%s, %s, %s)", 
                                    ('模糊修復', f'相似度匹配將 [{p}] 修正為 [{found_loc}]', text))
                            except Exception:
                                conn.rollback()
                            break
            
            # (4-4) 前綴與未知地點掃描
            if not found_loc:
                temp_text = smart_split_text(text_processed)
                raw_parts = re.split(r'[,\s、，]+', temp_text)
                for p in raw_parts:
                    p_clean = p.strip()
                    for loc in all_locs:
                        if p_clean.startswith(loc) and p_clean[len(loc):].strip().isalnum():
                             found_loc = p_clean.replace(" ", ""); ensure_location_exists_strict(conn, found_loc, base_loc=loc); text_processed = text_processed.replace(p, " "); break
                    if found_loc: break
                
                if not found_loc:
                    potential_loc = "未知地點"
                    for p in raw_parts:
                        p_clean = p.strip()
                        if len(p_clean) >= 2 and not p_clean.isdigit() and p_clean not in dynamic_known_members: 
                            potential_loc = p_clean; break


                    if potential_loc != "未知地點":
                        return f"⚠️ 找不到地點「{potential_loc}」，這筆我先沒記。\n▸ 新通路請先建檔：新增 {potential_loc} [金額]\n▸ 打錯字請用正確地點名重打一次"
                    else:
                        return "⚠️ 這筆我看不懂地點，先沒記。請用「日期 地點 人 金額」格式重打，或先用「新增 [地點] [金額]」建檔。"
                    if potential_loc != "未知地點":
                        learn_cost = manual_override if manual_override is not None and manual_override > 0 else 400
                        try:
                            cur.execute("SELECT 1 FROM locations WHERE location_name=%s", (potential_loc,))
                            if not cur.fetchone():
                                cur.execute("""INSERT INTO locations (location_name, weekday_cost, weekend_cost, category)
                                    VALUES (%s, %s, %s, '一般') ON CONFLICT (location_name) DO NOTHING""", 
                                    (potential_loc, learn_cost, learn_cost))
                                conn.commit()
                                is_newly_learned = True
                            found_loc = potential_loc
                            loc_data[found_loc] = {'rent': 0, 'clean': 0, 'cost': learn_cost, 'days': None, 'shared': None}
                            text_processed = text_processed.replace(potential_loc, " ", 1)
                        except Exception as e:
                            conn.rollback()

                    if not found_loc: return "❌ 系統無法辨識地點與人員，請重新確認格式。"

            # 5. 成員掃描
            new_members = []
            sorted_members = sorted(list(dynamic_known_members), key=len, reverse=True)
            for m in sorted_members:
                if m in text_processed:
                    new_members.append(m)
                    text_processed = text_processed.replace(m, " ")

            ## 6. 解析剩餘參數
            text_processed = smart_split_text(text_processed)
            clean_text = text_processed 
            raw_parts = re.split(r'[,\s、，]+', clean_text)
            parts = [p.strip() for p in raw_parts if p.strip()]

            multiplier = 1; surcharge_mod = 0; is_stocking = False; unit_count = 0

            for p in parts:
                p = p.strip().replace(',', '')
                if not p: continue
                if p.isdigit():
                    if len(p) == 8 and p.startswith('20'): continue
                    continue 
                
                if '進貨' in p: is_stocking = True; m_stock = re.search(r'(\d+)', p); manual_override = int(m_stock.group(1)) if m_stock else manual_override; continue
                m_unit = re.match(r'^(\d+)單$', p)
                if m_unit: unit_count = int(m_unit.group(1)); continue
                if p in ['兩格', '雙攤', '2格', '二格', 'x2']: multiplier = 2; continue
                if p in ['三格', '3格', '三攤', 'x3']: multiplier = 3; continue
                if re.match(r'^[x\*]\d+$', p.lower()): multiplier = int(p.lower().replace('x', '').replace('*', '')); continue
                
                m_add = re.match(r'^(加|公費|電費)\+?(\d+)$', p)
                if m_add: surcharge_mod += int(m_add.group(2)); continue
                
                m_sub = re.match(r'^(折|扣|減)(\d+)$', p)
                if m_sub: surcharge_mod -= int(m_sub.group(2)); continue
                
                if re.match(r'^[\+\-]\d+$', p): surcharge_mod += int(p); continue
                
                # 👇 就是這附近的縮排剛剛出錯了，現在已經完美對齊！
                # 💡 [防呆修復] 防止 safe_eval 把殘留的日期當成數學公式
                if re.search(r'[\*\/\(\)]', p) and not re.search(r'[^\d\+\-\*\/\(\)\s]', p) and not re.match(r'^\d{1,2}[/-]\d{1,2}$', p): 
                    val = safe_eval(p)
                    manual_override = val if val else manual_override
                    continue
                
                invalid_chars = [':', '：', '/', '🔺', '▲', '【', '】', '(', ')', '（', '）']
                if p != COMPANY_NAME and not p.isdigit() and not any(c in p for c in invalid_chars):
                    if p in dynamic_known_members: 
                        new_members.append(p)
                    # 👇 [疫苗 2] 把 '板橋', '中強強', '強', '早', '晚', 攤位備註 都加進攔截網！
                    elif len(p) <= 3 and p not in ['好', 'ok', 'OK', '市集', '進貨', '單', '指定', '沿用', '月租', '平日', '假日', '中正', '饒河', '夜市', '市場', '商圈', '廣場', '是唷', '中', '中強', '中強強', '強', '中偏', '板橋', '板橋體驗', '早', '晚', '角位', '角攤', '邊角'] and p not in all_locs:
                        new_members.append(p)
            
            if is_stocking: new_members = [] 
            if not new_members and not is_stocking: return f"❌ 未指定成員\n💡 提示：地點「{found_loc}」。"

            # 7. 檢查現有專案與處理別名映射
            cur.execute("SELECT project_id, total_fixed_cost, original_msg FROM projects WHERE record_date=%s AND location_name=%s", (record_date, found_loc))
            proj = cur.fetchone()
            pid = None
            
            mapped_new_members = set()
            for m in new_members:
                mapped_new_members.add(mem_alias_map.get(m, m))
            final_members = mapped_new_members 
            
            if proj:
                pid = proj[0]; existing_cost = proj[1]; existing_msg = proj[2]
                cur.execute("SELECT member_name FROM project_members WHERE project_id=%s", (pid,))
                existing_members_db = {row[0] for row in cur.fetchall()}
                final_members = existing_members_db.union(final_members)
                
                if manual_override is not None: final_cost = manual_override; note = "(更新指定)"
                else: final_cost = existing_cost; note = "(沿用)"
                
                new_combined_msg = f"{existing_msg} | {text}"
                cur.execute("UPDATE projects SET total_fixed_cost = %s, original_msg = %s WHERE project_id = %s", (final_cost, new_combined_msg, pid))
                cur.execute("DELETE FROM records WHERE project_id=%s", (pid,))
                cur.execute("DELETE FROM project_members WHERE project_id=%s", (pid,))
            else:
                rent = loc_data.get(found_loc, {}).get('rent', 0)
                clean = loc_data.get(found_loc, {}).get('clean', 0)
                base_cost = loc_data.get(found_loc, {}).get('cost', 400)
                biz_days = loc_data.get(found_loc, {}).get('days', "")
                shared_list_str = loc_data.get(found_loc, {}).get('shared', "")

                is_ghost_day = any(m.lower() in ['幽靈', 'ghost'] for m in final_members)

                if manual_override is not None: final_cost = manual_override; note = "(指定)"
                elif unit_count > 0: final_cost = (base_cost * unit_count) + surcharge_mod; note = f"({unit_count}單)"
                elif rent > 0:
                    effective_days = calculate_effective_days(record_date.year, record_date.month, biz_days)
                    daily_rent = round(rent / effective_days) if effective_days else 0
                    if is_ghost_day:
                        final_cost = daily_rent + surcharge_mod
                        note = f"(幽靈天/{effective_days}天)"
                        if shared_list_str: final_members = set([m.strip() for m in shared_list_str.split(',') if m.strip()])
                    else:
                        final_cost = daily_rent + clean + surcharge_mod
                        note = f"(月租/{effective_days}天)"
                else:
                    final_cost = (base_cost * multiplier) + surcharge_mod; note = ""
                    if multiplier > 1: note += f"x{multiplier}"
                
                cur.execute("INSERT INTO projects (record_date, location_name, total_fixed_cost, original_msg) VALUES (%s, %s, %s, %s) RETURNING project_id", (record_date, found_loc, final_cost, text))
                pid = cur.fetchone()[0]

            # 8. 寫入
            final_members_list = list(final_members)
            
            cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (COMPANY_NAME,))

            for m in final_members_list: 
                cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (m,))
                cur.execute("INSERT INTO project_members (project_id, member_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (pid, m))

            if is_stocking: comp = final_cost; per = 0
            elif final_members_list: 
                per = (final_cost // 2) // len(final_members_list)
                comp = final_cost - (per * len(final_members_list))
            else: comp = final_cost; per = 0

            cur.execute("INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg) VALUES (%s, %s, %s, %s, %s)", (record_date, COMPANY_NAME, pid, comp, text))
            for m in final_members_list: 
                cur.execute("INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg) VALUES (%s, %s, %s, %s, %s)", (record_date, m, pid, per, text))
            
            conn.commit()
            
            members_str = ", ".join(final_members_list)
            reply_msg = f"✅ 紀錄 {found_loc} 完成\n📅 {record_date.strftime('%m/%d')}\n🧑‍🤝‍🧑 成員: {members_str}\n💰 總成本: {final_cost} {note}{healing_note}\n🏢 公司負擔: {comp}\n👤 夥伴自付: {per} (每人)"
            
            if is_newly_learned:
                if manual_override is not None and manual_override > 0:
                    reply_msg += f"\n\n💡 【新地點建檔】系統已自動將「{found_loc}」預設成本記為 {learn_cost} 元！\n(若這是舊地點打錯字，請用：`設定別名 {found_loc} 正確地點`)"
                else:
                    reply_msg += f"\n\n⚠️ 【新地點提醒】系統已暫時預設「{found_loc}」成本為 400 元。\n👉 請設定正確成本：`新增 {found_loc} [平日價] [假日價]`\n👉 若只是打錯字：`設定別名 {found_loc} 正確地點`"
            
            return reply_msg
            
    except Exception as e: 
            conn.rollback()
            # 💡 [黑盒子啟動] 把害系統當機的「奇葩輸入」和「報錯原因」存起來
            try:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO error_logs (error_type, error_message, original_input) 
                        VALUES (%s, %s, %s)""", ('系統崩潰', str(e), text))
                    conn.commit()
            except: 
                # 原本這裡是 pass
                # 👉 請改成這樣：
                conn.rollback()
            return f"❌ 系統錯誤: {e}"
    finally: 
        close_db_connection(conn)