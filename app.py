import os
import re
from datetime import datetime, date, timedelta
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
COMPANY_NAME = os.getenv('COMPANY_NAME', '公司') # 公司的名稱/代號/固定分攤方

# 初始化 Flask App 和 LINE BOT API
app = Flask(__name__)
if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and DATABASE_URL):
    app.logger.error("關鍵環境變數未設定。請檢查 LINE_CHANNEL_ACCESS_TOKEN/SECRET 和 DATABASE_URL。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 2. 資料庫連接與初始化 (V7.1 結構 - 動態權重) ---

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
    初始化資料庫表格 (V7.1 結構 - 適用動態權重)。
    """
    conn = get_db_connection()
    if not conn:
        return
        
    try:
        with conn.cursor() as cur:
            
            if force_recreate:
                app.logger.warning("❗❗❗ 正在執行強制刪除並重建所有表格。資料將遺失。❗❗❗")
                # 依賴順序刪除
                cur.execute("DROP TABLE IF EXISTS records;")
                cur.execute("DROP TABLE IF EXISTS project_members;")
                cur.execute("DROP TABLE IF EXISTS projects;") 
                cur.execute("DROP TABLE IF EXISTS monthly_settlements;") 
                cur.execute("DROP TABLE IF EXISTS locations;")
                cur.execute("DROP TABLE IF EXISTS monthly_items;") 
                cur.execute("DROP TABLE IF EXISTS members;")
            
            # 4. 月度成本項目設定表 (移除 default_cost, 僅保留人名)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_items (
                    item_name VARCHAR(50) PRIMARY KEY,
                    default_members TEXT NOT NULL, 
                    memo TEXT
                );
            """)
            
            # 1. 地點設定表 (新增 open_days, 移除 weekend_cost - 簡化連動)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS locations (
                    location_name VARCHAR(50) PRIMARY KEY,
                    weekday_cost INTEGER NOT NULL, -- 作為單次活動成本
                    open_days TEXT, -- 營業日 (0=週日, 1=週一... 6=週六, 以逗號分隔)
                    linked_monthly_item VARCHAR(50) REFERENCES monthly_items(item_name) ON DELETE SET NULL 
                );
            """)

            # 2. 成員名單表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    name VARCHAR(50) PRIMARY KEY
                );
            """)

            # 3. 專案/活動表 (移除 member_cost_pool)
            cur.execute("""
                CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; 
                CREATE TABLE IF NOT EXISTS projects (
                    project_id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
                    record_date DATE NOT NULL,
                    location_name VARCHAR(50) REFERENCES locations(location_name) ON DELETE RESTRICT,
                    total_fixed_cost INTEGER NOT NULL, -- 紀錄實際攤提的總金額
                    original_msg TEXT
                );
            """)
            
            # 5. 月度成本實際結算表 (新增 total_capacity 作為總權重)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_settlements (
                    id SERIAL PRIMARY KEY,
                    item_name VARCHAR(50) REFERENCES monthly_items(item_name) ON DELETE RESTRICT,
                    settlement_date DATE NOT NULL, 
                    cost_amount INTEGER NOT NULL, -- 實際結算費用
                    actual_members TEXT NOT NULL, 
                    total_capacity INTEGER NOT NULL, -- 總潛在權重數
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

            # 7. 費用紀錄表
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
            
        conn.commit()
        app.logger.info("資料庫初始化完成或已存在 (V7.1)。")
    except Exception as e:
        conn.rollback()
        app.logger.error(f"資料庫初始化失敗: {e}") 
    finally:
        if conn: conn.close()

# ⚠️ 注意: 請手動確認此處設定為 False，以保留您現有的測試數據
init_db(force_recreate=False) 

# --- 3. Webhook 處理 (V7.1 分派邏輯) ---
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
    """處理傳入的文字訊息，並分派給對應的處理函數，並過濾雜訊。(V7.1)"""
    original_text = event.message.text.strip()
    reply_token = event.reply_token
    response = ""

    # --- 雜訊過濾機制 ---
    is_management_command = original_text.startswith(('新增', '刪除', '清單', '統計', '結算', '報表', '覆蓋', '測試', '修改'))
    record_match = re.search(r'(\d{1,2}/\d{1,2}[\(\（]\w[\)\）])\s+([^\s]+.*)|([^\s]+.*)', original_text)
    
    if not is_management_command and not record_match:
        return 'OK' 

    # --- 進入指令處理流程 ---
    try:
        if is_management_command:
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
            elif text.startswith('覆蓋'): 
                response = handle_location_coverage(text)
            elif original_text == '測試':
                response = "Bot 正常運作中！資料庫連接狀態良好。"
            else:
                response = "無法識別的管理指令。"
                
        elif record_match:
            record_text = original_text # V7.1: 將整行訊息傳入，讓解析函數處理日期省略邏輯
            response = handle_record_expense(record_text)
        else:
             response = "無法識別的指令格式。請檢查您的指令是否正確。"
            
    except Exception as e:
        app.logger.error(f"處理指令失敗: {e}")
        response = f"指令處理發生未知錯誤: {e}"

    if not response:
        response = "處理過程中發生未預期的錯誤，請檢查指令格式或回報問題。"

    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=response)
    )

# --- 4. 核心功能實現 (V7.1 邏輯) ---

# [C] 日期解析 (V7.1 - 支援日期省略)
def parse_record_command(text: str):
    """解析費用紀錄指令，支援日期省略，並檢查是否包含 '標準' 標籤或手動金額。"""
    
    # 嘗試匹配日期格式 (例如 11/12(三))
    date_match = re.match(r'^(\d{1,2}/\d{1,2})[\(\（](\w)[\)\）]', text)
    
    if date_match:
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
        
    else:
        # V7.1: 如果沒有匹配到日期，則使用當前電腦日期
        full_date = date.today()
        remaining_text = text.strip() 

    # 1. 檢查 '標準' 關鍵字 
    is_standard_mode = False
    temp_text = remaining_text.lower()
    
    # 過濾常見的雜訊詞 (可根據實際情況擴充)
    FILTER_WORDS = ['好', '是的', '可以', 'ok', '沒問題', '活動']
    
    if temp_text.endswith('標準'):
        is_standard_mode = True
        remaining_text = remaining_text[:-2].strip() 

    # 2. 檢查手動金額
    manual_cost = None
    cost_match = re.search(r'\s(\d+)$', remaining_text)
    if cost_match:
        manual_cost = int(cost_match.group(1))
        remaining_text = remaining_text[:cost_match.start()].strip() 
    
    parts = [p for p in remaining_text.split() if p not in FILTER_WORDS] 
    
    if len(parts) < 2:
        # V7.1: 如果沒有日期，第一個應該是地點，後面是人名
        # 但如果只有一個詞，無法判斷是地點還是人名
        if not date_match and len(parts) < 1:
             return None, "指令太短。請至少指定一個地點和一位人名 (或只指定地點，系統會假設您是分攤人)。"
        elif not date_match and len(parts) == 1:
             # 假設這個詞是地點，但沒有分攤人 (這是不允許的)
             return None, "請指定至少一位分攤人名。"
        
    location_name = parts[0]
    member_names = []
    if len(parts) > 1:
        member_names = parts[1:]
    
    if COMPANY_NAME in member_names:
        return None, f"請勿在紀錄中包含 {COMPANY_NAME}，它會自動加入計算。"

    return {
        'full_date': full_date,
        'member_names': member_names,
        'location_name': location_name,
        'manual_cost': manual_cost,
        'is_standard_mode': is_standard_mode
    }, None

# 輔助函數: 獲取地點成本與連動項目 (V7.1 - 獲取 open_days)
def get_location_details(conn, location_name):
    """獲取地點的成本、連動項目和營業日 (V7.1)"""
    try:
        with conn.cursor() as cur:
            # V7.1: 移除 weekend_cost，連動月項目不再需要 default_cost
            cur.execute("SELECT weekday_cost, linked_monthly_item, open_days FROM locations WHERE location_name = %s", (location_name,))
            result = cur.fetchone()
        
        if not result: return None
        activity_cost, linked_item_name, open_days = result
                 
        return activity_cost, linked_item_name, open_days
    except Exception as e:
        app.logger.error(f"獲取地點成本失敗: {e}")
        return None

# [D] 費用紀錄功能 (Project-Based V7.1 - 動態權重攤提)
def handle_record_expense(text: str) -> str:
    """處理費用紀錄指令，實作動態權重攤提邏輯。"""
    parsed_data, error = parse_record_command(text)
    if error:
        return f"❌ 指令解析失敗: {error}"
        
    full_date = parsed_data['full_date']
    new_members = parsed_data['member_names'] 
    location_name = parsed_data['location_name']
    manual_cost = parsed_data['manual_cost']
    is_standard_mode = parsed_data['is_standard_mode']

    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"
    
    if not new_members:
        return "❌ 紀錄失敗：請至少指定一位分攤人名。"

    try:
        with conn.cursor() as cur:
            # 1. 檢查該地點/日期是否已有專案 (Project)
            cur.execute("""
                SELECT p.project_id, p.total_fixed_cost
                FROM projects p 
                WHERE p.record_date = %s AND p.location_name = %s;
            """, (full_date, location_name))
            
            project_data = cur.fetchone()

            # 獲取地點詳細信息
            location_details = get_location_details(conn, location_name)
            if location_details is None:
                return f"❌ 地點 '{location_name}' 不存在或尚未設定。"
            
            C_activity, linked_item_name, open_days = location_details
            
            # 如果有手動金額，則覆蓋活動成本 C_activity
            C_activity = manual_cost if manual_cost is not None else C_activity 
            
            # --- 核心邏輯 V7.1: 連動的攤提單位改為 '單日權重' ---
            
            C_fixed_weight = 0 # 固定成本的權重
            should_link = linked_item_name and not is_standard_mode
            
            if should_link:
                # V7.1: 如果連動，則該次活動的固定成本權重為 1 (代表 1 天)
                C_fixed_weight = 1 
            
            C_total = C_activity # 總成本僅為活動成本，固定成本在月結算時才攤提餘額

            # --- 情況 B: 專案不存在 (初次紀錄/Project Lead) ---
            if not project_data:
                
                all_sharers = new_members + [COMPANY_NAME]
                total_sharers = len(all_sharers) 
                
                # 活動成本按人頭平分 (標準半價邏輯)
                C_activity_per_person = C_activity // 2 // len(new_members) if len(new_members) > 0 else 0
                C_company_activity = (C_activity - C_activity_per_person * len(new_members))
                
                # 寫入 Project 紀錄
                cur.execute("""
                    INSERT INTO projects (record_date, location_name, total_fixed_cost, original_msg)
                    VALUES (%s, %s, %s, %s) RETURNING project_id;
                """, (full_date, location_name, C_total, text))
                project_id = cur.fetchone()[0]

                # 寫入 Project Members
                for member in new_members:
                    cur.execute("INSERT INTO project_members (project_id, member_name) VALUES (%s, %s);", (project_id, member))
                    
                # 寫入 Records 紀錄
                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                    VALUES (%s, %s, %s, NULL, %s, %s);
                """, (full_date, COMPANY_NAME, project_id, C_company_activity, text))

                for member in new_members:
                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, NULL, %s, %s);
                    """, (full_date, member, project_id, C_activity_per_person, text))
                
                conn.commit()
                
                link_note = f"\n(固定成本已標記為 1 權重，將在月結算時動態抵扣)" if should_link else ""
                
                return f"""(完美！) {full_date.strftime('%Y-%m-%d')} 的 {location_name} 專案已紀錄！
(金額資訊) 活動總成本：{C_total:,} 元。{link_note}
--------------------------------
> {COMPANY_NAME} 應攤提活動成本：{C_company_activity:,} 元
> {len(new_members)} 位夥伴 每人應攤提：{C_activity_per_person:,} 元
(小提醒) 如果有連動月成本，請在月結算時輸入實際金額和總潛在權重數。"""

            # --- 情況 A: 專案已存在 (只處理加入成員) ---
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
                
                # 重新計算攤提 (活動成本按標準半價邏輯)
                N = len(all_business_members)
                C_activity_total = total_fixed_cost
                
                C_activity_per_person = C_activity_total // 2 // N if N > 0 else 0
                C_company_activity = (C_activity_total - C_activity_per_person * N)

                # 寫入新增的成員
                for member in members_to_add:
                    cur.execute("""
                        INSERT INTO project_members (project_id, member_name) VALUES (%s, %s) 
                        ON CONFLICT (project_id, member_name) DO NOTHING;
                    """, (project_id, member))

                # 刪除並重寫 Records (確保攤提金額更新)
                cur.execute("DELETE FROM records WHERE project_id = %s;", (project_id,))
                
                # 重寫 COMPANY_NAME 紀錄
                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                    VALUES (%s, %s, %s, NULL, %s, %s);
                """, (full_date, COMPANY_NAME, project_id, C_company_activity, text))

                # 重寫業務員紀錄
                for member in all_business_members:
                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, monthly_settlement_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, NULL, %s, %s);
                    """, (full_date, member, project_id, C_activity_per_person, text))
                
                conn.commit()
                return f"""✅ 成功加入新成員至 {location_name} ({full_date.strftime('%Y-%m-%d')}) 專案。
--------------------------------
活動總成本: {C_activity_total:,} 元。總分攤人數已更新為 {len(all_business_members)} 位。
每人應攤提費用: {C_activity_per_person:,}
{COMPANY_NAME} 應攤提費用: {C_company_activity:,}"""
        
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
        
# [A] 新增/更新功能 (V7.1 - 處理 open_days)
def handle_management_add(text: str) -> str:
    """處理 新增 人名/地點 指令 (V7.1 - 處理 open_days)"""
    parts = text.split()
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            # 處理：新增人名 [人名] (不變)
            if len(parts) == 2 and parts[0] == '新增人名':
                member_name = parts[1]
                cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (member_name,))
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 已成功新增成員：{member_name}。"
                else:
                    return f"💡 成員 {member_name} 已存在。"

            # 處理：新增 地點 [地點名] [成本] 連動 [月項目名] 營業日 [一,三,五,六,日] (V7.1 新增)
            elif len(parts) >= 8 and parts[1] == '地點' and parts[4] == '連動' and parts[6] == '營業日':
                loc_name = parts[2]
                cost_val = int(parts[3])
                linked_item = parts[5]
                open_days_str = parts[7]
                
                # 檢查連動月項目是否存在
                cur.execute("SELECT item_name FROM monthly_items WHERE item_name = %s;", (linked_item,))
                if cur.fetchone() is None:
                    return f"❌ 連動失敗：月成本項目「{linked_item}」不存在。請先使用 '新增 月項目 [名稱] [人名...]' 設定。"
                
                # 轉換營業日文字為數字 (0=週日, 1=週一, ..., 6=週六)
                day_map = {'日': '0', '一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6'}
                open_days_codes = []
                for day_char in open_days_str.split(','):
                    if day_char in day_map:
                        open_days_codes.append(day_map[day_char])
                
                if not open_days_codes:
                    return "❌ 營業日格式錯誤。請使用逗號分隔，例如: 一,三,五,六,日"

                open_days_db = ','.join(open_days_codes)

                cur.execute("""
                    INSERT INTO locations (location_name, weekday_cost, linked_monthly_item, open_days)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (location_name) DO UPDATE SET weekday_cost = EXCLUDED.weekday_cost, linked_monthly_item = EXCLUDED.linked_monthly_item, open_days = EXCLUDED.open_days;
                """, (loc_name, cost_val, linked_item, open_days_db))
                conn.commit()
                
                return f"""✅ 地點「{loc_name}」已設定成功。
活動成本：{cost_val} 元。
連動月成本：「{linked_item}」 (單日權重 1)。
自動營業日：{open_days_str} (將用於月結算自動計算權重)。"""

            # 處理：新增 地點 [地點名] [成本] (單一費率，無連動)
            elif len(parts) == 4 and parts[1] == '地點':
                loc_name, cost_val = parts[2], int(parts[3])
                cur.execute("""
                    INSERT INTO locations (location_name, weekday_cost, linked_monthly_item, open_days)
                    VALUES (%s, %s, NULL, NULL)
                    ON CONFLICT (location_name) DO UPDATE SET weekday_cost = EXCLUDED.weekday_cost, linked_monthly_item = EXCLUDED.linked_monthly_item, open_days = EXCLUDED.open_days;
                """, (loc_name, cost_val))
                conn.commit()
                return f"✅ 地點「{loc_name}」已設定成功，單次活動成本 {cost_val} (標準分攤，無連動/營業日設定)。"

            else:
                return "❌ 新增 指令格式錯誤。請參考最新的 V7.1 指令格式。"

    except ValueError:
        return "❌ 成本金額必須是數字。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"新增指令資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# [H] 新增月度成本項目設定 (V7.1 - 移除基礎金額)
def handle_management_add_monthly_item(text: str) -> str:
    """處理 新增 月項目 [項目名] [人名1] [人名2]... 指令 (V7.1 - 移除基礎金額)"""
    parts = text.split()
    
    if len(parts) < 3 or parts[0] != '新增' or parts[1] != '月項目':
        return "❌ 新增月項目格式錯誤。請使用: 新增 月項目 [項目名] [人名1] [人名2]..."

    item_name = parts[2]
    member_names = parts[3:]
    memo = f"月度固定成本：{item_name} (動態權重攤提)"
    
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
                INSERT INTO monthly_items (item_name, default_members, memo)
                VALUES (%s, %s, %s)
                ON CONFLICT (item_name) DO UPDATE SET default_members = EXCLUDED.default_members, memo = EXCLUDED.memo;
            """, (item_name, member_list_str, memo))
            
            action = "更新" if cur.rowcount == 0 else "新增"
            conn.commit()
            
            return f"""✅ 成功{action}月成本項目「{item_name}」。
--------------------------------
類型: 動態權重攤提 (V7.1)
預設分攤人 (含 {COMPANY_NAME}): {member_list_str.replace(',', '、')}
(下一步) 請記得連動地點時設定「營業日」！"""

    except Exception as e:
        conn.rollback()
        app.logger.error(f"新增月項目資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# 輔助函數: 計算當月符合特定營業日的天數 (V7.1 核心邏輯)
def calculate_days_in_month(target_year, target_month, open_days_str):
    """根據營業日清單計算當月符合的天數。"""
    if not open_days_str:
        return 0
        
    open_days = [int(d) for d in open_days_str.split(',')]
    start_date = date(target_year, target_month, 1)
    
    # 找到下個月的第一天來確定這個月的結束日期
    if target_month == 12:
        end_date = date(target_year + 1, 1, 1)
    else:
        end_date = date(target_year, target_month + 1, 1)

    delta = timedelta(days=1)
    current_date = start_date
    count = 0
    
    while current_date < end_date:
        # weekday() 返回 0 (週一) 到 6 (週日)。我們使用 0=週日, 1=週一... 6=週六
        # 因此需要轉換: (current_date.weekday() + 1) % 7
        day_of_week = (current_date.weekday() + 1) % 7 
        if day_of_week in open_days:
            count += 1
        current_date += delta
        
    return count

# [I] 月度成本實際結算 (V7.1 - 動態權重分配)
def handle_settle_monthly_cost(text: str) -> str:
    """處理月成本實際結算指令 (V7.1 - 動態權重分配)"""
    parts = text.split()
    # 結算 月項目 [月份] [項目名] [實際金額] [總潛在權重數_選填] [人名選填]
    if len(parts) < 5 or parts[0] != '結算' or parts[1] != '月項目':
        return "❌ 結算月項目格式錯誤。\n結算 月項目 [月份 (如 11月)] [項目名] [實際金額] [總潛在權重數_選填] [人名選填]"
        
    month_str = parts[2].replace('月', '').strip()
    item_name = parts[3]
    
    try:
        target_month = int(month_str)
        cost_amount = int(parts[4])
    except ValueError:
        return "❌ 月份或金額必須是有效的數字。"
        
    # 獲取總潛在權重數 (V7.1: 可選，如果沒提供，則自動計算連動地點的)
    total_capacity_manual = None
    if len(parts) > 5:
        try:
            total_capacity_manual = int(parts[5])
        except ValueError:
            # 如果第五部分不是數字，則視為是人名
            pass
    
    specified_members = parts[6:] if total_capacity_manual is not None else parts[5:]

    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT default_members FROM monthly_items WHERE item_name = %s;", (item_name,))
            item_data = cur.fetchone()
            if not item_data:
                return f"❌ 找不到月成本項目「{item_name}」。請先使用 '新增 月項目' 設定。"
            
            default_members_str = item_data[0]
            default_members = default_members_str.split(',') if default_members_str else []
            
            # 確認最終分攤人
            final_members = [n for n in (specified_members if specified_members else default_members) if n != COMPANY_NAME]
            final_members = [n for n in final_members if n]
            
            if not final_members:
                return "❌ 無法結算。分攤人名單不能為空。"
                
            current_date = date.today()
            current_year = current_date.year
            
            # 調整跨年月份 (修正 V7.1 原始邏輯中的錯誤判斷，確保年份準確)
            # 1. 處理年末到年初的跳年 (例如: 11/12月結算1月，應為下一年度)
            if target_month == 1 and current_date.month in (11, 12):
                 current_year += 1
            # 2. 處理年初結算去年底的月份 (例如: 1/2月結算12月，應為上一年度)
            elif target_month == 12 and current_date.month in (1, 2):
                 current_year -= 1
            # 3. 否則，預設為當前年度的月份
            
            settlement_date = date(current_year, target_month, 1)

            # 1. 獲取所有連動到該月項目的地點及其營業日
            cur.execute("SELECT location_name, open_days FROM locations WHERE linked_monthly_item = %s;", (item_name,))
            linked_locations_data = cur.fetchall()
            
            # 2. 計算總潛在權重數 (Total Capacity)
            total_capacity_calculated = 0
            if linked_locations_data:
                for loc_name, open_days_str in linked_locations_data:
                    # 依據地點的營業日，自動計算當月潛在天數
                    days_in_month = calculate_days_in_month(current_year, target_month, open_days_str)
                    total_capacity_calculated += days_in_month
            
            # 如果用戶手動提供了總潛在權重數，則使用手動數值
            total_capacity = total_capacity_manual if total_capacity_manual is not None else total_capacity_calculated
            
            if total_capacity <= 0:
                 return "❌ 結算失敗：總潛在權重數為 0。請檢查連動地點的營業日設定，或手動提供總潛在權重數。"

            # 3. 計算單日權重單價 (C_unit)
            C_unit = cost_amount / total_capacity 
            
            # 4. 統計實際活動權重 (已抵扣部分)
            # 查找當月已紀錄的連動專案天數 (每個專案算 1 個權重)
            linked_location_names = [loc[0] for loc in linked_locations_data]
            
            cur.execute("""
                SELECT COUNT(p.project_id) FROM projects p
                WHERE p.location_name = ANY(%s)
                  AND date_part('month', p.record_date) = %s;
            """, (linked_location_names, target_month))
            
            linked_activity_days = cur.fetchone()[0]
            
            # 活動抵扣總額 = 實際活動天數 * 單日權重單價
            total_deducted_cost = round(linked_activity_days * C_unit)
            
            # 5. 計算最終攤提餘額 (Final Cost to Settle)
            final_cost_to_settle = cost_amount - total_deducted_cost
            
            if final_cost_to_settle < 0:
                 return f"💡 月成本『{item_name}』結算金額 {cost_amount:,} 元，被連動活動扣除 {total_deducted_cost:,} 元後，實際無需攤提 (已全數攤提或超額攤提)。"

            if final_cost_to_settle == 0:
                 return f"✅ 月成本『{item_name}』結算金額 {cost_amount:,} 元，因 {linked_activity_days} 天活動已在日常中分攤，實際無需再攤提。"
            
            # --- 執行結算 (按人頭數平分餘額) ---
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
                INSERT INTO monthly_settlements (item_name, settlement_date, cost_amount, actual_members, total_capacity, original_msg)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;
            """, (item_name, final_cost_to_settle, cost_amount, actual_members_str, total_capacity, text))
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
            
            return f"""✅ 成功{action} {target_month} 月份月成本結算：『{item_name}』
--------------------------------
實際總金額: {cost_amount:,} 元
總潛在權重 (自動計算/手動輸入): {total_capacity} 天
單日權重單價: 約 {C_unit:,.2f} 元/天
活動抵扣總額 ({linked_activity_days} 天): {total_deducted_cost:,} 元
最終攤提餘額: {final_cost_to_settle:,} 元 (按 {total_sharers} 人平分)
--------------------------------
每位業務員攤提: {cost_per_sharer:,} 元
{COMPANY_NAME} 攤提: {company_cost:,} 元"""
        
    except psycopg2.errors.ForeignKeyViolation:
        conn.rollback()
        return f"❌ 結算失敗：找不到月成本項目「{item_name}」。請先使用 '新增 月項目' 設定。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"結算月項目資料庫錯誤: {e}")
        return f"❌ 處理結算月項目發生錯誤: {e}"
    finally:
        if conn: conn.close()

# [B], [F], [J], [K] (清單、刪除、報表、覆蓋率) 函數邏輯與 V6.9/V7.1 保持一致 (此處省略以節省篇幅，但需確保在您的 app.py 完整保留)

# [E] 費用統計功能 (V7.1 - 支援月份省略)
def handle_management_stat(text: str) -> str:
    """處理費用統計指令 (V7.1 - 支援月份省略)"""
    parts = text.split()
    
    if len(parts) == 1 or parts[0] != '統計':
        return "❌ 統計指令格式錯誤。請使用: 統計 [人名/公司] [月份 (例如 9月, 可省略)]。"

    target_name = parts[1]
    
    if len(parts) == 2:
        # V7.1: 如果沒有提供月份，使用當前月份
        target_month = date.today().month
    else:
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
            
            action_verb = "需要攤提" if target_name != COMPANY_NAME else "總共支出"

            return f"""--- {target_name} {target_month} 月份總費用快報 ---
在這個月，{target_name} {action_verb} 的費用總額是：
# {total_cost:,} 元 #
(此金額包含日常活動和月度固定成本的攤提部分)"""

    except Exception as e:
        app.logger.error(f"統計指令資料庫錯誤: {e}")
        return f"❌ 查詢統計數據發生錯誤: {e}"
    finally:
        if conn: conn.close()

# --- 5. Flask App 運行 ---
if __name__ == "__main__":
    pass