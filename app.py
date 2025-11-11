import os
import re
from datetime import datetime, date
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import psycopg2
from psycopg2 import sql

# --- 1. 環境變數與設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
DATABASE_URL = os.getenv('DATABASE_URL')
COMPANY_NAME = os.getenv('COMPANY_NAME', 'BOSS') # 公司的名稱/代號

# 初始化 Flask App 和 LINE BOT API
app = Flask(__name__)
if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and DATABASE_URL):
    app.logger.error("關鍵環境變數未設定。請檢查 LINE_CHANNEL_ACCESS_TOKEN/SECRET 和 DATABASE_URL。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 2. 資料庫連接與初始化 (V6.5 結構 - 修正 SQL 註釋) ---

def get_db_connection():
    """建立並返回資料庫連接"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        app.logger.error(f"資料庫連接失敗: {e}")
        return None

def init_db(force_recreate=False):
    """
    初始化資料庫表格 (V6.5 結構)。
    """
    conn = get_db_connection()
    if not conn:
        return
        
    try:
        with conn.cursor() as cur:
            
            if force_recreate:
                app.logger.warning("❗❗❗ 正在執行強制刪除並重建所有表格以修正 Schema。資料將遺失。❗❗❗")
                # 依賴順序刪除
                cur.execute("DROP TABLE IF EXISTS records;")
                cur.execute("DROP TABLE IF EXISTS project_members;")
                cur.execute("DROP TABLE IF EXISTS projects;") 
                cur.execute("DROP TABLE IF EXISTS monthly_settlements;") 
                cur.execute("DROP TABLE IF EXISTS locations;")
                cur.execute("DROP TABLE IF EXISTS monthly_items;") # 先刪除 locations/monthly_settlements 的外鍵
                cur.execute("DROP TABLE IF EXISTS members;")
            
            # 4. 月度成本項目設定表 (包含 default_cost)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_items (
                    item_name VARCHAR(50) PRIMARY KEY,
                    default_cost INTEGER NOT NULL, 
                    default_members TEXT NOT NULL, 
                    memo TEXT
                );
            """)
            
            # 1. 地點設定表 (包含 linked_monthly_item 外鍵)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS locations (
                    location_name VARCHAR(50) PRIMARY KEY,
                    weekday_cost INTEGER NOT NULL,
                    weekend_cost INTEGER NOT NULL,
                    linked_monthly_item VARCHAR(50) REFERENCES monthly_items(item_name) ON DELETE SET NULL 
                );
            """)

            # 2. 成員名單表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    name VARCHAR(50) PRIMARY KEY
                );
            """)

            # 3. 專案/活動表
            cur.execute("""
                CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; 
                CREATE TABLE IF NOT EXISTS projects (
                    project_id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
                    record_date DATE NOT NULL,
                    location_name VARCHAR(50) REFERENCES locations(location_name) ON DELETE RESTRICT,
                    total_fixed_cost INTEGER NOT NULL,
                    member_cost_pool INTEGER NOT NULL,
                    original_msg TEXT
                );
            """)
            
            # 5. 月度成本實際結算表 (修復: # 改為 --)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_settlements (
                    id SERIAL PRIMARY KEY,
                    item_name VARCHAR(50) REFERENCES monthly_items(item_name) ON DELETE RESTRICT,
                    settlement_date DATE NOT NULL, 
                    cost_amount INTEGER NOT NULL, -- 注意：此處儲存的是最終攤提金額
                    actual_members TEXT NOT NULL, 
                    original_msg TEXT,
                    UNIQUE (settlement_date, item_name)
                );
            """)
            
            # 6. 專案參與成員表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS project_members (
                    project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE,
                    member_name VARCHAR(50) REFERENCES members(name) ON DELETE CASCADE,
                    PRIMARY KEY (project_id, member_name)
                );
            """)

            # 7. 費用紀錄表 (修復: # 改為 --)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    id SERIAL PRIMARY KEY,
                    record_date DATE NOT NULL,
                    member_name VARCHAR(50) REFERENCES members(name) ON DELETE CASCADE,
                    project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE NULL,
                    monthly_settlement_id INTEGER REFERENCES monthly_settlements(id) ON DELETE CASCADE NULL,
                    cost_paid INTEGER NOT NULL,
                    original_msg TEXT,
                    
                    CONSTRAINT chk_one_id_not_null CHECK (
                        (project_id IS NOT NULL AND monthly_settlement_id IS NULL) OR 
                        (project_id IS NULL AND monthly_settlement_id IS NOT NULL)
                    )
                );
            """)
            
            # 確保公司成員存在
            cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (COMPANY_NAME,))
            
            # --- 預設數據 (如果需要自動初始化，可在此添加) ---
            # ... 
            
        conn.commit()
        app.logger.info("資料庫初始化完成或已存在 (V6.5)。")
    except Exception as e:
        conn.rollback()
        # 由於錯誤訊息中包含 syntax error at or near "#"，我們現在修復了，但還是記錄錯誤
        app.logger.error(f"資料庫初始化失敗: {e}") 
    finally:
        if conn: conn.close()

# ⚠️ 注意: 請手動確認此處設定為 False，以保留您現有的測試數據
init_db(force_recreate=False) 

# --- 3. Webhook 處理 ---
@app.route("/callback", methods=['POST'])
def callback():
    """處理 LINE Webhook 傳來的 POST 請求"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature.")
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """處理傳入的文字訊息，並分派給對應的處理函數"""
    original_text = event.message.text.strip()
    reply_token = event.reply_token
    response = ""

    try:
        record_match = re.search(r'(\d{1,2}/\d{1,2}[\(\（]\w[\)\）])\s+([^\s]+.*)', original_text)
        
        # 處理管理指令
        if original_text.startswith('新增') or original_text.startswith('刪除') or \
           original_text.startswith('清單') or original_text.startswith('統計') or \
           original_text.startswith('結算') or original_text.startswith('報表') or \
           original_text.startswith('出席'): # 🌟 新增出席指令判斷
            
            text = original_text.split('\n')[0].strip() 
            
            if text.startswith('新增 月項目'):
                response = handle_management_add_monthly_item(text)
            elif text.startswith('新增'):
                response = handle_management_add(text)
            elif text.startswith('刪除'):
                response = handle_management_delete(text)
            elif text.startswith('清單'):
                response = handle_management_list(text)
            elif text.startswith('統計'):
                response = handle_management_stat(text)
            elif text.startswith('結算 月項目'):
                response = handle_settle_monthly_cost(text)
            elif text.startswith('報表'): 
                response = handle_report(text)
            elif text.startswith('出席'): # 🌟 新增出席指令分派
                response = handle_attendance_report(text)
            else:
                response = "無法識別的管理指令。"

        elif original_text == '測試':
            response = "Bot 正常運作中！資料庫連接狀態良好。"
        elif record_match:
            record_text = record_match.group(1) + " " + record_match.group(2)
            response = handle_record_expense(record_text)
        else:
            response = "無法識別的指令格式。請輸入 '清單 地點' 或 '9/12(五) 人名 地點' (v6.5)。"
            
    except Exception as e:
        app.logger.error(f"處理指令失敗: {e}")
        response = f"指令處理發生未知錯誤: {e}"

    if not response:
        response = "處理過程中發生未預期的錯誤，請檢查指令格式或回報問題。"

    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=response)
    )

# --- 4. 核心功能實現 (V6.5 邏輯) ---

# [C] 日期解析 (V6.5 修正: 新增 is_standard_mode 標記)
def parse_record_command(text: str):
    """解析費用紀錄指令，檢查是否包含 '標準' 標籤或手動金額。"""
    date_match = re.match(r'^(\d{1,2}/\d{1,2})[\(\（](\w)[\)\）]', text)
    if not date_match:
        return None, "日期格式錯誤 (月/日(星期))"

    record_date_str = date_match.group(1) 
    today = date.today()
    current_year = today.year
    input_month = int(record_date_str.split('/')[0])
    
    # 跨年判斷
    record_year = current_year
    if today.month == 12 and input_month == 1:
        record_year = current_year + 1
    elif today.month == 1 and input_month == 12:
        record_year = current_year - 1
        
    try:
        full_date = datetime.strptime(f'{record_year}/{record_date_str}', '%Y/%m/%d').date()
    except ValueError:
        return None, "日期不存在 (例如 2月30日)"
    
    remaining_text = text[date_match.end():].strip() 
    
    # 1. 檢查 '標準' 關鍵字 (V6.5 新增)
    is_standard_mode = False
    temp_text = remaining_text.lower()
    
    FILTER_WORDS = ['好', '桌5布4燈1', '架1']
    
    # 檢查是否以 '標準' 結尾
    if temp_text.endswith('標準'):
        is_standard_mode = True
        remaining_text = remaining_text[:-2].strip() # 移除 '標準'

    # 2. 檢查手動金額
    manual_cost = None
    cost_match = re.search(r'\s(\d+)$', remaining_text)
    if cost_match:
        manual_cost = int(cost_match.group(1))
        remaining_text = remaining_text[:cost_match.start()].strip() 
    
    parts = [p for p in remaining_text.split() if p not in FILTER_WORDS] 
    
    if len(parts) < 2:
        return None, "請至少指定一位人名和一個地點"

    member_names = [parts[0]] 
    location_name = parts[1]  
    
    if len(parts) > 2:
        member_names.extend(parts[2:])

    if COMPANY_NAME in member_names:
        return None, f"請勿在紀錄中包含 {COMPANY_NAME}，它會自動加入計算。"

    return {
        'full_date': full_date,
        'day_of_week': date_match.group(2), 
        'member_names': member_names,
        'location_name': location_name,
        'manual_cost': manual_cost,
        'is_standard_mode': is_standard_mode # 🌟 V6.5 回傳是否為標準模式
    }, None

# 輔助函數: 獲取地點成本與連動項目
def get_location_details(conn, location_name, full_date):
    """根據日期和地點獲取成本和連動項目"""
    is_weekend = (full_date.weekday() >= 5) 
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT weekday_cost, weekend_cost, linked_monthly_item FROM locations WHERE location_name = %s", (location_name,))
            result = cur.fetchone()
        
        if not result: return None
        weekday_cost, weekend_cost, linked_item_name = result
        
        activity_cost = weekend_cost if is_weekend else weekday_cost
        return activity_cost, linked_item_name
    except Exception as e:
        app.logger.error(f"獲取地點成本失敗: {e}")
        return None

# [D] 費用紀錄功能 (Project-Based V6.5 修正 - 處理 '標準' 模式)
def handle_record_expense(text: str) -> str:
    """處理費用紀錄指令，實作連動地點和平分/標準模式切換。"""
    parsed_data, error = parse_record_command(text)
    if error:
        return f"❌ 指令解析失敗: {error}"
        
    full_date = parsed_data['full_date']
    new_members = parsed_data['member_names'] 
    location_name = parsed_data['location_name']
    manual_cost = parsed_data['manual_cost']
    is_standard_mode = parsed_data['is_standard_mode'] # 🌟 V6.5: 獲取模式

    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            # 1. 檢查該地點/日期是否已有專案 (Project)
            cur.execute("""
                SELECT p.project_id, p.total_fixed_cost
                FROM projects p 
                WHERE p.record_date = %s AND p.location_name = %s;
            """, (full_date, location_name))
            
            project_data = cur.fetchone()

            # --- 情況 B: 專案不存在 (初次紀錄/Project Lead) ---
            if not project_data:
                # 獲取地點詳細信息
                location_details = get_location_details(conn, location_name, full_date)
                if location_details is None:
                    return f"❌ 地點 '{location_name}' 不存在或尚未設定。"
                
                C_activity, linked_item_name = location_details
                
                # 如果有手動金額，則覆蓋活動成本 C_activity
                C_activity = manual_cost if manual_cost is not None else C_activity 
                
                # 判斷是否執行連動邏輯：地點有連動項目 AND 不是標準模式 (V6.5 核心)
                should_link = linked_item_name and not is_standard_mode

                if should_link:
                    # --- 核心邏輯 A: 連動月成本地點 (例如 總站) ---
                    cur.execute("SELECT default_cost FROM monthly_items WHERE item_name = %s;", (linked_item_name,))
                    fixed_cost_data = cur.fetchone()
                    if not fixed_cost_data:
                         return f"❌ 找不到連動月成本項目「{linked_item_name}」的固定金額。請檢查設定。"

                    C_fixed = fixed_cost_data[0] 
                    C_total = C_activity + C_fixed 
                    
                    all_sharers = new_members + [COMPANY_NAME]
                    total_sharers = len(all_sharers) 
                    
                    C_share_per_person = C_total // total_sharers
                    remainder = C_total % total_sharers
                    
                    C_company_final = C_share_per_person + remainder
                    # member_cost_pool 設為 C_total 方便後續對帳
                    
                    # 寫入 Project 紀錄 (記錄總成本 C_total)
                    cur.execute("""
                        INSERT INTO projects (record_date, location_name, total_fixed_cost, member_cost_pool, original_msg)
                        VALUES (%s, %s, %s, %s, %s) RETURNING project_id;
                    """, (full_date, location_name, C_total, C_total, text))
                    project_id = cur.fetchone()[0]

                    # 寫入 Project Members
                    for member in new_members:
                        cur.execute("INSERT INTO project_members (project_id, member_name) VALUES (%s, %s);", (project_id, member))
                        
                    # 寫入 Records 紀錄
                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, NULL, %s, %s);
                    """, (full_date, COMPANY_NAME, project_id, C_company_final, text))

                    for member in new_members:
                        cur.execute("""
                            INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                            VALUES (%s, %s, %s, NULL, %s, %s);
                        """, (full_date, member, project_id, C_share_per_person, text))
                    
                    conn.commit()
                    
                    return f"""✅ 啟動 {location_name} 專案 ({full_date.strftime('%m/%d')})。
--------------------------------
活動成本: {C_activity:,} + 固定成本({linked_item_name}): {C_fixed:,} = 總成本 {C_total:,}。
由 {len(new_members)} 位業務員和 BOSS 平分 (共 {total_sharers} 份)。
每人應攤提費用: {C_share_per_person:,}
{COMPANY_NAME} 攤提: {C_company_final:,} (含餘數 {remainder})
💡 注意：此費用已包含月固定成本，該項目在月結時將會自動扣除。"""

                # --- 核心邏輯 B: 標準地點/標準模式 (無連動月成本 或 啟動標準模式) ---
                else:
                    C = C_activity
                    N = len(new_members)
                    C_unit_total = C // 2
                    remainder_total = C % 2 
                    
                    C_company_stage1 = C_unit_total + remainder_total
                    member_cost_pool = C_unit_total
                    
                    C_member_individual = 0
                    remainder_members = 0
                    
                    if N > 0:
                        C_member_individual = member_cost_pool // N
                        remainder_members = member_cost_pool % N
                        
                    C_company_final = C_company_stage1 + remainder_members

                    # 寫入 Project 紀錄 (total_fixed_cost 記錄活動成本 C, member_cost_pool 記錄攤給業務員的份額 C_unit_total)
                    cur.execute("""
                        INSERT INTO projects (record_date, location_name, total_fixed_cost, member_cost_pool, original_msg)
                        VALUES (%s, %s, %s, %s, %s) RETURNING project_id;
                    """, (full_date, location_name, C, member_cost_pool, text))
                    project_id = cur.fetchone()[0]

                    for member in new_members:
                        cur.execute("INSERT INTO project_members (project_id, member_name) VALUES (%s, %s);", (project_id, member))

                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, NULL, %s, %s);
                    """, (full_date, COMPANY_NAME, project_id, C_company_final, text))

                    for member in new_members:
                        cur.execute("""
                            INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                            VALUES (%s, %s, %s, NULL, %s, %s);
                        """, (full_date, member, project_id, C_member_individual, text))
                    
                    conn.commit()
                    
                    mode_note = " (標準模式)" if is_standard_mode else ""
                    return f"""✅ 啟動 {location_name} 專案 ({full_date.strftime('%m/%d')}){mode_note}。總成本 {C:,}。
--------------------------------
公司 ({COMPANY_NAME}) 應攤提費用: {C_company_final:,}
{len(new_members)} 位業務員 每人應攤提費用: {C_member_individual:,}
💡 後續相同日期/地點的紀錄，請以相同格式輸入，將會自動加入此專案分攤。"""

            # --- 情況 A: 專案已存在 (只處理加入成員，攤提邏輯不變) ---
            else:
                project_id, total_fixed_cost = project_data
                
                cur.execute("""
                    SELECT member_name FROM project_members WHERE project_id = %s;
                """, (project_id,))
                current_members = [row[0] for row in cur.fetchall()]
                
                members_to_add = [m for m in new_members if m not in current_members]
                
                if not members_to_add and len(new_members) > 0:
                    return f"💡 {location_name} 在 {full_date.strftime('%m/%d')} 的紀錄已存在，且所有指定成員都已加入分攤名單。"

                all_business_members = sorted(list(set(current_members) | set(new_members)))
                
                # 重新計算攤提 (使用 project 儲存的 total_fixed_cost)
                N = len(all_business_members)
                
                # 專案已存在，則攤提總人數為 N + 1
                total_sharers = N + 1 
                
                C_share_per_person = total_fixed_cost // total_sharers
                remainder = total_fixed_cost % total_sharers

                C_company_final = C_share_per_person + remainder
                
                # 寫入新增的成員
                for member in members_to_add:
                    cur.execute("""
                        INSERT INTO project_members (project_id, member_name) VALUES (%s, %s) 
                        ON CONFLICT (project_id, member_name) DO NOTHING;
                    """, (project_id, member))

                # 刪除並重寫 Records (確保攤提金額更新)
                cur.execute("DELETE FROM records WHERE project_id = %s;", (project_id,))
                
                # 重寫 BOSS 紀錄
                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                    VALUES (%s, %s, %s, NULL, %s, %s);
                """, (full_date, COMPANY_NAME, project_id, C_company_final, text))

                # 重寫業務員紀錄
                for member in all_business_members:
                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, NULL, %s, %s);
                    """, (full_date, member, project_id, C_share_per_person, text))
                
                conn.commit()
                return f"""✅ 成功加入新成員至 {location_name} ({full_date.strftime('%m/%d')}) 專案。
--------------------------------
總成本: {total_fixed_cost:,}。總分攤人數已更新為 {total_sharers} 位。
每人應攤提費用: {C_share_per_person:,}
{COMPANY_NAME} 應攤提費用: {C_company_final:,} (含餘數 {remainder})"""
        
    except ValueError:
        conn.rollback()
        return "❌ 金額格式錯誤。"
    except psycopg2.errors.ForeignKeyViolation as fke:
        conn.rollback()
        return f"❌ 紀錄失敗：人名或地點不存在。請先使用 '新增人名' 或 '新增 地點'。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"費用紀錄資料庫錯誤: {e}")
        return f"❌ 處理費用紀錄發生錯誤: {e}"
    finally:
        if conn: conn.close()
        
# [A] 新增/更新功能
def handle_management_add(text: str) -> str:
    """處理 新增 人名/地點 指令"""
    parts = text.split()
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            # 處理：新增人名 [人名] (共 2 部分)
            if len(parts) == 2 and parts[0] == '新增人名':
                member_name = parts[1]
                cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (member_name,))
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 已成功新增成員：{member_name}。"
                else:
                    return f"💡 成員 {member_name} 已存在。"

            # 處理：新增 地點 [地點名] [成本] (單一費率，共 4 部分)
            elif len(parts) == 4 and parts[1] == '地點':
                loc_name, cost_val = parts[2], int(parts[3])
                cur.execute("""
                    INSERT INTO locations (location_name, weekday_cost, weekend_cost, linked_monthly_item)
                    VALUES (%s, %s, %s, NULL)
                    ON CONFLICT (location_name) DO UPDATE SET weekday_cost = EXCLUDED.weekday_cost, weekend_cost = EXCLUDED.weekend_cost, linked_monthly_item = EXCLUDED.linked_monthly_item;
                """, (loc_name, cost_val, cost_val))
                conn.commit()
                return f"✅ 地點「{loc_name}」已設定成功，平日/假日成本皆為 {cost_val} (標準分攤)。"

            # 處理：新增 地點 [地點名] [平日成本] [假日成本] (雙費率，共 5 部分)
            elif len(parts) == 5 and parts[1] == '地點':
                loc_name = parts[2]
                weekday_cost_val = int(parts[3])
                weekend_cost_val = int(parts[4])
                
                cur.execute("""
                    INSERT INTO locations (location_name, weekday_cost, weekend_cost, linked_monthly_item)
                    VALUES (%s, %s, %s, NULL)
                    ON CONFLICT (location_name) DO UPDATE SET weekday_cost = EXCLUDED.weekday_cost, weekend_cost = EXCLUDED.weekend_cost, linked_monthly_item = EXCLUDED.linked_monthly_item;
                """, (loc_name, weekday_cost_val, weekend_cost_val))
                conn.commit()
                return f"✅ 地點「{loc_name}」已設定成功，平日 {weekday_cost_val}，假日 {weekend_cost_val} (標準分攤)。"
            
            # 處理：新增 地點 [地點名] [成本] 連動 [月項目名] (共 6 部分)
            elif len(parts) == 6 and parts[1] == '地點' and parts[4] == '連動':
                loc_name = parts[2]
                cost_val = int(parts[3])
                linked_item = parts[5]
                
                # 檢查連動月項目是否存在
                cur.execute("SELECT item_name FROM monthly_items WHERE item_name = %s;", (linked_item,))
                if cur.fetchone() is None:
                    return f"❌ 連動失敗：月成本項目「{linked_item}」不存在。請先使用 '新增 月項目 [名稱] [金額] [人名...]' 設定。"

                cur.execute("""
                    INSERT INTO locations (location_name, weekday_cost, weekend_cost, linked_monthly_item)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (location_name) DO UPDATE SET weekday_cost = EXCLUDED.weekday_cost, weekend_cost = EXCLUDED.weekend_cost, linked_monthly_item = EXCLUDED.linked_monthly_item;
                """, (loc_name, cost_val, cost_val, linked_item))
                conn.commit()
                return f"""✅ 地點「{loc_name}」已設定成功，單次活動成本 {cost_val}，
並連動月成本項目「{linked_item}」。當日發生時，總成本平分給所有參與者與 BOSS。
💡 欲強制標準分攤 (只攤活動成本)，請在指令末尾加上 **標準**。"""

            else:
                return "❌ 新增 地點/人名 指令格式錯誤。\n新增人名 [人名]\n新增 地點 [地點名] [成本](單一/標準)\n新增 地點 [地點名] [成本] 連動 [月項目名](連動)"

    except ValueError:
        return "❌ 成本金額必須是數字。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"新增指令資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# [H] 新增月度成本項目設定 
def handle_management_add_monthly_item(text: str) -> str:
    """處理 新增 月項目 [項目名] [金額] [人名1] [人名2]... 指令"""
    parts = text.split()
    
    if len(parts) < 5 or parts[0] != '新增' or parts[1] != '月項目':
        return "❌ 新增月項目格式錯誤。請使用: 新增 月項目 [項目名] [金額] [人名1] [人名2]..."

    item_name = parts[2]
    
    try:
        default_cost = int(parts[3]) # 基礎固定金額
    except ValueError:
        return "❌ 金額必須是數字。"
        
    member_names = parts[4:]
    memo = f"月度固定成本：{item_name} (基礎: {default_cost})"
    
    if not member_names:
        return "❌ 請至少指定一位預設分攤人名。"
    
    member_names = [n for n in member_names if n != COMPANY_NAME]
    member_list_str = ','.join(member_names)

    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            for name in member_names:
                cur.execute("SELECT name FROM members WHERE name = %s", (name,))
                if cur.fetchone() is None:
                    return f"❌ 成員 {name} 不存在。請先使用 '新增人名'。"

            cur.execute("""
                INSERT INTO monthly_items (item_name, default_cost, default_members, memo)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (item_name) DO UPDATE SET default_cost = EXCLUDED.default_cost, default_members = EXCLUDED.default_members, memo = EXCLUDED.memo;
            """, (item_name, default_cost, member_list_str, memo))
            
            action = "更新" if cur.rowcount == 0 else "新增"
            conn.commit()
            
            return f"""✅ 成功{action}月成本項目「{item_name}」。
--------------------------------
基礎固定金額: {default_cost:,} 元
預設分攤人 (含 {COMPANY_NAME}): {member_list_str.replace(',', '、')}"""

    except Exception as e:
        conn.rollback()
        app.logger.error(f"新增月項目資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# [I] 新增月度成本實際結算 (包含連動對帳邏輯)
def handle_settle_monthly_cost(text: str) -> str:
    """處理月成本實際結算指令"""
    parts = text.split()
    if len(parts) < 5 or parts[0] != '結算' or parts[1] != '月項目':
        return "❌ 結算月項目格式錯誤。\n結算 月項目 [月份 (如 11月)] [項目名] [實際金額] [人名選填 (覆蓋預設)]"
        
    month_str = parts[2].replace('月', '').strip()
    item_name = parts[3]
    
    try:
        target_month = int(month_str)
        cost_amount = int(parts[4])
    except ValueError:
        return "❌ 月份或金額必須是有效的數字。"
        
    specified_members = parts[5:]

    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT default_members, default_cost FROM monthly_items WHERE item_name = %s;", (item_name,))
            item_data = cur.fetchone()
            if not item_data:
                return f"❌ 找不到月成本項目「{item_name}」。請先使用 '新增 月項目' 設定。"
            
            default_members_str, default_cost = item_data
            default_members = default_members_str.split(',') if default_members_str else []
            
            if specified_members:
                for name in specified_members:
                    cur.execute("SELECT name FROM members WHERE name = %s", (name,))
                    if cur.fetchone() is None:
                        return f"❌ 指定成員 {name} 不存在。請先使用 '新增人名'。"
                final_members = [n for n in specified_members if n != COMPANY_NAME]
            else:
                final_members = default_members
                
            final_members = [n for n in final_members if n]
            
            if not final_members:
                return "❌ 無法結算。分攤人名單不能為空。"
                
            current_year = date.today().year
            if target_month < date.today().month and date.today().month == 12:
                 current_year += 1
            
            settlement_date = date(current_year, target_month, 1)

            # --- V6.5/V6.4 自動扣除連動活動已攤提的固定費用 (對帳機制) ---
            cur.execute("SELECT location_name FROM locations WHERE linked_monthly_item = %s;", (item_name,))
            linked_locations = [row[0] for row in cur.fetchall()]
            
            total_fixed_cost_deducted = 0
            
            if linked_locations:
                # 查找當月已紀錄的連動專案天數
                cur.execute("""
                    SELECT COUNT(p.project_id) FROM projects p
                    WHERE p.location_name = ANY(%s)
                      AND date_part('month', p.record_date) = %s
                      AND p.member_cost_pool = p.total_fixed_cost; 
                """, (linked_locations, target_month))
                
                linked_activity_days = cur.fetchone()[0]
                
                if linked_activity_days > 0:
                    total_fixed_cost_deducted = linked_activity_days * default_cost
            
            # 3. 計算最終攤提金額
            final_cost_to_settle = cost_amount - total_fixed_cost_deducted
            
            if final_cost_to_settle < 0:
                 return f"💡 月成本『{item_name}』結算金額 {cost_amount:,} 元，被連動活動扣除 {total_fixed_cost_deducted:,} 元後，實際無需攤提 (已全數攤提或超額攤提)。"

            if final_cost_to_settle == 0:
                 return f"✅ 月成本『{item_name}』結算金額 {cost_amount:,} 元，因 {linked_activity_days} 天活動已在日常中分攤，實際無需再攤提。"
            
            # --- 執行結算 ---
            all_sharers = final_members + [COMPANY_NAME]
            total_sharers = len(all_sharers)
            
            cost_per_sharer = final_cost_to_settle // total_sharers
            remainder = final_cost_to_settle % total_sharers
            
            company_cost = cost_per_sharer + remainder
            
            # 避免重複結算，先刪後插
            cur.execute("SELECT id FROM monthly_settlements WHERE settlement_date = %s AND item_name = %s;", 
                        (settlement_date, item_name))
            old_settlement_id_data = cur.fetchone()
            
            if old_settlement_id_data:
                cur.execute("DELETE FROM monthly_settlements WHERE id = %s;", (old_settlement_id_data[0],))

            actual_members_str = ','.join(final_members)
            cur.execute("""
                INSERT INTO monthly_settlements (item_name, settlement_date, cost_amount, actual_members, original_msg)
                VALUES (%s, %s, %s, %s, %s) RETURNING id;
            """, (item_name, settlement_date, final_cost_to_settle, actual_members_str, text))
            monthly_settlement_id = cur.fetchone()[0]

            # 寫入 Records
            cur.execute("""
                INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                VALUES (%s, %s, NULL, %s, %s, %s);
            """, (settlement_date, COMPANY_NAME, monthly_settlement_id, company_cost, text))

            for member in final_members:
                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                    VALUES (%s, %s, NULL, %s, %s, %s);
                """, (settlement_date, member, monthly_settlement_id, cost_per_sharer, text))
            
            conn.commit()
            
            action = "更新" if old_settlement_id_data else "新增"
            member_list_display = actual_members_str.replace(',', '、')
            deduct_note = f"\n(已自動扣除 {linked_activity_days} 天活動的費用，共 {total_fixed_cost_deducted:,} 元)" if total_fixed_cost_deducted > 0 else ""
            
            return f"""✅ 成功{action} {target_month} 月份月成本結算：『{item_name}』{deduct_note}
--------------------------------
最終攤提成本: {final_cost_to_settle:,} 元
實際分攤人 (共 {total_sharers} 位): {member_list_display}、{COMPANY_NAME}
每位業務員攤提: {cost_per_sharer:,} 元
{COMPANY_NAME} 攤提: {company_cost:,} 元 (含餘數 {remainder})"""
        
    except psycopg2.errors.ForeignKeyViolation:
        conn.rollback()
        return f"❌ 結算失敗：找不到月成本項目「{item_name}」。請先使用 '新增 月項目' 設定。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"結算月項目資料庫錯誤: {e}")
        return f"❌ 處理結算月項目發生錯誤: {e}"
    finally:
        if conn: conn.close()

# [B] 清單查詢功能
def handle_management_list(text: str) -> str:
    """處理清單指令"""
    parts = text.split()
    if len(parts) != 2 or parts[0] != '清單':
        return "❌ 清單指令格式錯誤。請使用: 清單 人名, 清單 地點, 清單 月項目 或 清單 月結算。"
        
    list_type = parts[1].lower()
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            if list_type == '人名':
                cur.execute("SELECT name FROM members ORDER BY name;")
                members = [row[0] for row in cur.fetchall()]
                if not members: return "📋 目前沒有任何已設定的人名或業務員。"
                member_list_str = "、".join(members)
                return f"📋 **現有成員 (業務員/公司):**\n{member_list_str}"

            elif list_type == '地點':
                cur.execute("SELECT location_name, weekday_cost, weekend_cost, linked_monthly_item FROM locations ORDER BY location_name;")
                locations = cur.fetchall()
                
                if not locations: return "📋 目前沒有任何已設定的地點。"

                response = "📋 **現有地點及其成本:**\n"
                for name, weekday_cost, weekend_cost, linked_item in locations:
                    linked_str = f" [連動: {linked_item}]" if linked_item else ""
                    if weekday_cost == weekend_cost:
                        response += f"• {name}: {weekday_cost} (單一費率){linked_str}\n"
                    else:
                        response += f"• {name}: 平日 {weekday_cost} / 假日 {weekend_cost}{linked_str}\n"
                response += "\n💡 紀錄時加 **標準** 可強制標準分攤。"
                return response.strip()

            elif list_type == '月項目': 
                cur.execute("SELECT item_name, default_cost, default_members FROM monthly_items ORDER BY item_name;")
                monthly_items = cur.fetchall()
                
                if not monthly_items: return "📋 目前沒有任何已設定的月度成本項目。"

                response = "📋 **現有月度成本項目 (固定費用/預設分攤):**\n"
                for item_name, default_cost, default_members in monthly_items:
                    members = default_members.replace(',', '、')
                    response += f"• {item_name}: 基礎費用 {default_cost:,} (預設人: {members}、{COMPANY_NAME})\n"
                return response.strip()

            elif list_type == '月結算':
                cur.execute("""
                    SELECT s.settlement_date, s.item_name, s.cost_amount, s.actual_members 
                    FROM monthly_settlements s 
                    ORDER BY s.settlement_date DESC, s.item_name;
                """)
                monthly_settlements = cur.fetchall()
                
                if not monthly_settlements: return "📋 目前沒有任何月度成本結算紀錄。"

                response = "📋 **現有月度成本結算紀錄 (實際攤提金額):**\n"
                for settlement_date, item_name, cost_amount, actual_members in monthly_settlements:
                    members = actual_members.replace(',', '、')
                    response += f"• {settlement_date.strftime('%Y/%m')} [{item_name}]: {cost_amount:,} 元 (實分人: {members}、{COMPANY_NAME})\n"
                return response.strip()
                
            else:
                return "❌ 無法識別的清單類別。請輸入 '清單 人名', '清單 地點', '清單 月項目' 或 '清單 月結算'。"

    except Exception as e:
        app.logger.error(f"清單指令資料庫錯誤: {e}")
        return f"❌ 查詢清單發生錯誤: {e}"
    finally:
        if conn: conn.close()
        
# [E] 費用統計功能
def handle_management_stat(text: str) -> str:
    """處理費用統計指令"""
    parts = text.split()
    if len(parts) != 3 or parts[0] != '統計':
        return "❌ 統計指令格式錯誤。請使用: 統計 [人名/公司] [月份 (例如 9月)]。"
        
    target_name = parts[1]
    month_str = parts[2].replace('月', '').strip()

    try:
        target_month = int(month_str)
        if not (1 <= target_month <= 12):
            raise ValueError
    except ValueError:
        return "❌ 月份格式錯誤。請輸入有效的數字月份 (1 到 12)。"
        
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM members WHERE name = %s", (target_name,))
            if cur.fetchone() is None:
                return f"❌ 無法統計。成員 {target_name} 不存在於名單中。"

            cur.execute("""
                SELECT SUM(cost_paid)
                FROM records r
                WHERE r.member_name = %s 
                  AND date_part('month', r.record_date) = %s;
            """, (target_name, target_month))
            
            total_cost = cur.fetchone()[0]
            
            if total_cost is None:
                return f"✅ {target_name} 在 {target_month} 月份沒有任何費用紀錄。"
            
            return f"📈 **{target_name} {target_month} 月份總費用統計**：\n總攤提費用為：**{total_cost:,}** 元 (含月度成本攤提)。"

    except Exception as e:
        app.logger.error(f"統計指令資料庫錯誤: {e}")
        return f"❌ 查詢統計數據發生錯誤: {e}"
    finally:
        if conn: conn.close()

# [J] 報表匯出功能 
def handle_report(text: str) -> str:
    """處理報表指令"""
    parts = text.split()
    if len(parts) != 2 or parts[0] != '報表':
        return "❌ 報表指令格式錯誤。請使用: 報表 [月份 (例如 11月)]。"

    month_str = parts[1].replace('月', '').strip()

    try:
        target_month = int(month_str)
        if not (1 <= target_month <= 12):
            raise ValueError
    except ValueError:
        return "❌ 月份格式錯誤。請輸入有效的數字月份 (1 到 12)。"
        
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    r.record_date, 
                    r.member_name, 
                    r.cost_paid, 
                    CASE
                        WHEN r.project_id IS NOT NULL THEN p.location_name
                        WHEN r.monthly_settlement_id IS NOT NULL THEN ms.item_name
                        ELSE '未知'
                    END AS item_name,
                    CASE
                        WHEN r.project_id IS NOT NULL THEN '活動攤提'
                        WHEN r.monthly_settlement_id IS NOT NULL THEN '月成本結算'
                        ELSE '未知'
                    END AS record_type,
                    COALESCE(p.total_fixed_cost, ms.cost_amount) AS total_cost_for_item
                FROM records r
                LEFT JOIN projects p ON r.project_id = p.project_id
                LEFT JOIN monthly_settlements ms ON r.monthly_settlement_id = ms.id
                WHERE date_part('month', r.record_date) = %s
                ORDER BY r.record_date, r.member_name;
            """, (target_month,))
            
            data = cur.fetchall()

            if not data:
                return f"✅ {target_month} 月份沒有任何詳細費用紀錄可以生成報表。"

            report_lines = []
            
            header = "日期\t紀錄類型\t項目/地點\t攤提人\t攤提金額\t項目總成本"
            report_lines.append(header)
            
            for row in data:
                record_date, member_name, cost_paid, item_name, record_type, total_cost_for_item = row
                
                cost_paid_str = f"{cost_paid:,}"
                total_cost_str = f"{total_cost_for_item:,}" if total_cost_for_item else ""

                line = f"{record_date.strftime('%Y/%m/%d')}\t{record_type}\t{item_name}\t{member_name}\t{cost_paid_str}\t{total_cost_str}"
                report_lines.append(line)
            
            cur.execute("""
                SELECT member_name, SUM(cost_paid)
                FROM records
                WHERE date_part('month', record_date) = %s
                GROUP BY member_name
                ORDER BY member_name;
            """, (target_month,))
            
            summary_data = cur.fetchall()
            
            summary_lines = ["\n--- 總結 (方便貼上試算表) ---\n"]
            summary_lines.append("攤提人\t總攤提金額")
            
            for member, total_cost in summary_data:
                summary_lines.append(f"{member}\t{total_cost:,}")

            final_report = f"📋 **{target_month} 月份費用明細報表** (請複製以下純文字表格，貼上 Excel/試算表):\n\n"
            final_report += "\n".join(report_lines)
            final_report += "\n\n"
            final_report += "\n".join(summary_lines)
            
            return final_report
            
    except Exception as e:
        app.logger.error(f"報表指令資料庫錯誤: {e}")
        return f"❌ 產生報表發生錯誤: {e}"
    finally:
        if conn: conn.close()

# [F] 刪除功能
def handle_management_delete(text: str) -> str:
    """處理刪除指令"""
    parts = text.split()
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"
    
    try:
        with conn.cursor() as cur:
            if len(parts) == 4 and parts[1] == '紀錄':
                date_part_str = parts[2]
                location_name = parts[3]
                
                temp_text = f"{date_part_str} 測試人名 {location_name}"
                parsed_date_data, _ = parse_record_command(temp_text) 
                
                if not parsed_date_data:
                    return "❌ 刪除紀錄指令的日期格式或地點名稱無效 (月/日(星期) 地點名)。"
                        
                record_date = parsed_date_data['full_date']

                cur.execute("""
                    SELECT project_id FROM projects
                    WHERE record_date = %s AND location_name = %s
                    LIMIT 1;
                """, (record_date, location_name))
                
                project_id_result = cur.fetchone()

                if not project_id_result:
                    return f"💡 找不到 {location_name} 在 {date_part_str} 的專案紀錄。"

                project_id = project_id_result[0]
                cur.execute("DELETE FROM projects WHERE project_id = %s;", (project_id,))
                
                conn.commit()
                return f"✅ 已成功刪除 {location_name} 在 {date_part_str} 的整個專案紀錄 (包含所有成員攤提)。"

            elif len(parts) == 3 and parts[1] == '月項目':
                item_name = parts[2]
                cur.execute("DELETE FROM monthly_items WHERE item_name = %s;", (item_name,))
                
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 已成功刪除月成本項目「{item_name}」及其相關的所有結算紀錄。"
                else:
                    return f"💡 找不到月成本項目「{item_name}」。"

            elif len(parts) == 4 and parts[1] == '月結算':
                month_str = parts[2].replace('月', '').strip()
                item_name = parts[3]
                
                try:
                    target_month = int(month_str)
                except ValueError:
                    return "❌ 月份格式錯誤，請輸入有效的數字月份 (如 11月)。"

                current_year = date.today().year
                if target_month < date.today().month and date.today().month == 12:
                    current_year += 1
                try:
                    settlement_date = date(current_year, target_month, 1)
                except ValueError:
                    return "❌ 無效的月份或年份計算錯誤。"

                cur.execute("DELETE FROM monthly_settlements WHERE settlement_date = %s AND item_name = %s;", 
                            (settlement_date, item_name))
                
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 已成功刪除 {target_month} 月份月成本項目「{item_name}」的結算紀錄。"
                else:
                    return f"💡 找不到 {target_month} 月份月成本項目「{item_name}」的結算紀錄。"

            elif len(parts) == 3 and parts[1] == '人名':
                member_name = parts[2]
                if member_name == COMPANY_NAME:
                    return f"❌ 無法刪除系統專用成員 {COMPANY_NAME}。"
                    
                cur.execute("DELETE FROM members WHERE name = %s;", (member_name,))
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 成員 {member_name} 已從名單中刪除。所有相關費用紀錄也已同步清除。" 
                else:
                    return f"💡 名單中找不到 {member_name}。"

            elif len(parts) == 3 and parts[1] == '地點':
                loc_name = parts[2]
                cur.execute("DELETE FROM locations WHERE location_name = %s;", (loc_name,))
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 地點 {loc_name} 已成功刪除。"
                else:
                    return f"💡 地點 {loc_name} 不存在。"
                    
            else:
                return "❌ 刪除指令格式錯誤。\n刪除 人名 [人名]\n刪除 地點 [地點名]\n刪除 紀錄 [月/日(星期)] [地點名]\n刪除 月項目 [項目名]\n刪除 月結算 [月份] [項目名]"

    except psycopg2.errors.RestrictViolation:
        conn.rollback()
        return "❌ 刪除失敗: 仍有專案紀錄或月結算引用此項目/地點。請先刪除相關的紀錄/結算。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"刪除指令資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# [K] 活動出席統計 (V6.5 新增)
def handle_attendance_report(text: str) -> str:
    """統計該月所有成員的出席活動天數和缺席天數。"""
    parts = text.split()
    if len(parts) != 2 or parts[0] != '出席':
        return "❌ 出席統計指令格式錯誤。請使用: 出席 [月份 (例如 11月)]。"

    month_str = parts[1].replace('月', '').strip()
    
    try:
        target_month = int(month_str)
        if not (1 <= target_month <= 12): raise ValueError
    except ValueError:
        return "❌ 月份格式錯誤。請輸入有效的數字月份 (1 到 12)。"
        
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            # 1. 查詢該月總活動天數 (排除月結算紀錄)
            cur.execute("""
                SELECT COUNT(DISTINCT record_date)
                FROM projects
                WHERE date_part('month', record_date) = %s;
            """, (target_month,))
            
            total_activity_days = cur.fetchone()[0]

            if total_activity_days == 0:
                return f"✅ {target_month} 月份沒有任何活動紀錄（專案）。"

            # 2. 查詢所有業務員 (排除 COMPANY_NAME)
            cur.execute("SELECT name FROM members WHERE name != %s ORDER BY name;", (COMPANY_NAME,))
            all_members = [row[0] for row in cur.fetchall()]
            
            # 3. 查詢該月每位成員的出席天數
            cur.execute("""
                SELECT 
                    pm.member_name, 
                    COUNT(DISTINCT p.record_date) AS days_attended
                FROM project_members pm
                JOIN projects p ON pm.project_id = p.project_id
                WHERE date_part('month', p.record_date) = %s
                GROUP BY pm.member_name
                ORDER BY pm.member_name;
            """, (target_month,))
            
            attendance_data = {row[0]: row[1] for row in cur.fetchall()}

            # 4. 彙整結果
            response = f"📋 **{target_month} 月份活動出席統計 (共 {total_activity_days} 天)**\n"
            
            for member in all_members:
                days_attended = attendance_data.get(member, 0)
                days_absent = total_activity_days - days_attended
                
                response += f"• **{member}**: 去 {days_attended} 天 / 不去 {days_absent} 天\n"
            
            response += f"\n(註: 此統計不包含 {COMPANY_NAME}，也不計入月成本結算日。)"

            return response.strip()

    except Exception as e:
        app.logger.error(f"出席統計資料庫錯誤: {e}")
        return f"❌ 查詢出席統計發生錯誤: {e}"
    finally:
        if conn: conn.close()

# --- 5. Flask App 運行 ---
if __name__ == "__main__":
    # 如果您需要在本地運行，可以取消註釋以下行
    # app.run(host='0.0.0.0', port=os.getenv('PORT', 5000))
    pass