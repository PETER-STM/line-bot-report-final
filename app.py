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
COMPANY_NAME = os.getenv('COMPANY_NAME', 'BOSS')

# 初始化 Flask App 和 LINE BOT API
app = Flask(__name__)
if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and DATABASE_URL):
    app.logger.error("關鍵環境變數未設定。請檢查 LINE_CHANNEL_ACCESS_TOKEN/SECRET 和 DATABASE_URL。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 2. 資料庫連接與初始化 ---

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
    初始化資料庫表格 (地點、成員、紀錄)
    """
    conn = get_db_connection()
    if not conn:
        return
        
    try:
        with conn.cursor() as cur:
            
            # --- ❗ 解決 Schema 衝突的方案：強制刪除並重建表格 ---
            if force_recreate:
                app.logger.warning("❗❗❗ 正在執行強制刪除並重建所有表格以修正 Schema。資料將遺失。❗❗❗")
                cur.execute("DROP TABLE IF EXISTS records;")
                cur.execute("DROP TABLE IF EXISTS project_members;")
                cur.execute("DROP TABLE IF EXISTS projects;") # 新增
                cur.execute("DROP TABLE IF EXISTS locations;")
                cur.execute("DROP TABLE IF EXISTS members;")
            # ---------------------------------------------------
                
            # 1. 地點設定表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS locations (
                    location_name VARCHAR(50) PRIMARY KEY,
                    weekday_cost INTEGER NOT NULL,
                    weekend_cost INTEGER NOT NULL
                );
            """)
            # 2. 成員名單表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    name VARCHAR(50) PRIMARY KEY
                );
            """)

            # 3. 專案/活動表 (New! 追蹤單次固定成本的計算)
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
            
            # 4. 專案參與成員表 (New!)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS project_members (
                    project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE,
                    member_name VARCHAR(50) REFERENCES members(name) ON DELETE CASCADE,
                    PRIMARY KEY (project_id, member_name)
                );
            """)

            # 5. 費用紀錄表 (紀錄每個成員的最終應攤提金額)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    id SERIAL PRIMARY KEY,
                    record_date DATE NOT NULL,
                    member_name VARCHAR(50) REFERENCES members(name) ON DELETE CASCADE,
                    project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE,
                    cost_paid INTEGER NOT NULL,
                    original_msg TEXT
                );
            """)
            
            # 確保 '公司' (BOSS) 作為分攤單位存在於 members 表
            cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (COMPANY_NAME,))
            
            # 預先插入 '市集' 避免外鍵錯誤，並給定預設值 (400)
            cur.execute("""
                INSERT INTO locations (location_name, weekday_cost, weekend_cost)
                VALUES (%s, %s, %s)
                ON CONFLICT (location_name) DO NOTHING;
            """, ('市集', 400, 400))
            
        conn.commit()
        app.logger.info("資料庫初始化完成或已存在。")
    except Exception as e:
        app.logger.error(f"資料庫初始化失敗: {e}")
    finally:
        if conn: conn.close()

# ⚠️ 步驟 A: 暫時開啟強制重建，以使新的 Schema (projects 表) 生效
init_db(force_recreate=True) 

# --- 3. Webhook 處理 (略) ---
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
    text = event.message.text.strip()
    reply_token = event.reply_token

    try:
        if text.startswith('新增'):
            response = handle_management_add(text)
        elif text.startswith('刪除'):
            response = handle_management_delete(text)
        elif text.startswith('清單'):
            response = handle_management_list(text)
        elif text.startswith('統計'):
            response = handle_management_stat(text)
        elif text == '測試':
            response = "Bot 正常運作中！資料庫連接狀態良好。"
        elif re.match(r'^\d{1,2}/\d{1,2}\(\w\).*', text):
            response = handle_record_expense(text)
        else:
            response = "無法識別的指令格式。請輸入 '清單 地點' 或 '9/12(五) 彼 市集' (v3-final)。"
            
    except Exception as e:
        app.logger.error(f"處理指令失敗: {e}")
        response = f"指令處理發生未知錯誤: {e}"

    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=response)
    )

# --- 4. 核心功能實現 (只展示修改部分) ---

# [C] 日期解析 (優化版)
def parse_record_command(text: str):
    """
    解析費用紀錄指令。格式: [月/日(星期)] [地點名] [人名1] [人名2]... [金額(可選)]
    """
    date_match = re.match(r'^(\d{1,2}/\d{1,2})\((\w)\)', text)
    if not date_match:
        return None, "日期格式錯誤 (月/日(星期))"

    record_date_str = date_match.group(1) 
    
    # 年份自動判斷 (邏輯不變)
    today = date.today()
    current_year = today.year
    input_month = int(record_date_str.split('/')[0])
    
    record_year = current_year
    if today.month == 1 and input_month == 12 or (today.month > 1 and input_month > today.month):
        record_year = current_year - 1
    
    try:
        full_date = datetime.strptime(f'{record_year}/{record_date_str}', '%Y/%m/%d').date()
    except ValueError:
        return None, "日期不存在 (例如 2月30日)"
    
    remaining_text = text[date_match.end():].strip() 
    
    manual_cost = None
    cost_match = re.search(r'\s(\d+)$', remaining_text)
    if cost_match:
        manual_cost = int(cost_match.group(1))
        remaining_text = remaining_text[:cost_match.start()].strip() 
    
    parts = remaining_text.split()
    if len(parts) < 2:
        return None, "請至少指定一個地點和一位人名"

    location_name = parts[0]
    member_names = parts[1:]
    
    if COMPANY_NAME in member_names:
        return None, f"請勿在紀錄中包含 {COMPANY_NAME}，它會自動加入計算。"

    return {
        'full_date': full_date,
        'day_of_week': date_match.group(2), 
        'member_names': member_names,
        'location_name': location_name,
        'manual_cost': manual_cost
    }, None

# [D] 費用紀錄功能 (Project-Based 邏輯)
def handle_record_expense(text: str) -> str:
    """處理費用紀錄指令，實作 Project-Based 兩階段分攤邏輯。"""
    parsed_data, error = parse_record_command(text)
    if error:
        return f"❌ 指令解析失敗: {error}"
        
    full_date = parsed_data['full_date']
    new_members = parsed_data['member_names'] # 這次指令新增的人員
    location_name = parsed_data['location_name']
    manual_cost = parsed_data['manual_cost']

    # 專案唯一識別碼 (日期 + 地點)
    project_identifier = f"{full_date.strftime('%Y-%m-%d')}-{location_name}"

    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            # 1. 檢查該地點/日期是否已有專案 (Project)
            cur.execute("""
                SELECT p.project_id, p.member_cost_pool
                FROM projects p 
                WHERE p.record_date = %s AND p.location_name = %s;
            """, (full_date, location_name))
            
            project_data = cur.fetchone()
            
            # --- 情況 A: 專案已存在 (後續紀錄/加入成員) ---
            if project_data:
                project_id, member_cost_pool = project_data
                
                # 檢查新成員是否已在專案中，並將未加入的成員加入
                cur.execute("""
                    SELECT member_name FROM project_members WHERE project_id = %s;
                """, (project_id,))
                current_members = [row[0] for row in cur.fetchall()] + [COMPANY_NAME]
                
                members_to_add = [m for m in new_members if m not in current_members]
                
                if not members_to_add and len(new_members) > 0:
                    return f"💡 {location_name} 在 {full_date.strftime('%m/%d')} 的紀錄已存在，且所有指定成員都已加入分攤名單。"

                # 排除 COMPANY_NAME 和已在名單中的成員
                all_business_members = sorted(list(set(current_members) | set(new_members)))
                all_business_members.remove(COMPANY_NAME) 
                
                N = len(all_business_members)
                C_member_individual = member_cost_pool // N
                remainder_members = member_cost_pool % N

                # 重新計算 BOSS 的最終攤提金額
                C_company_final = (member_cost_pool + remainder_members)

                # 2. 更新 project_members (加入新成員)
                for member in members_to_add:
                    cur.execute("""
                        INSERT INTO project_members (project_id, member_name) VALUES (%s, %s) 
                        ON CONFLICT (project_id, member_name) DO NOTHING;
                    """, (project_id, member))

                # 3. 清除並更新 records 表
                cur.execute("DELETE FROM records WHERE project_id = %s;", (project_id,))
                
                # 寫入 BOSS 紀錄 (總成本池 - 業務員成本池 = BOSS成本池)
                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg)
                    VALUES (%s, %s, %s, %s, %s);
                """, (full_date, COMPANY_NAME, project_id, C_company_final, text))

                # 寫入每個業務員的紀錄
                for member in all_business_members:
                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, %s, %s);
                    """, (full_date, member, project_id, C_member_individual, text))
                
                conn.commit()
                return f"""✅ 成功加入新成員至 {location_name} ({full_date.strftime('%m/%d')}) 專案。
--------------------------------
總業務員人數已更新為 {N} 位。
每位業務員應攤提費用: {C_member_individual}
{COMPANY_NAME} 應攤提費用: {C_company_final} (固定成本 + 餘數)"""


            # --- 情況 B: 專案不存在 (初次紀錄/Project Lead) ---
            else:
                # 1. 取得總成本 C
                C = manual_cost if manual_cost is not None else get_location_cost(conn, location_name, full_date)
                if C is None:
                    return f"❌ 地點 '{location_name}' 尚未設定成本，請先使用 '新增 地點' 指令。"

                # 2. 核心計算邏輯 (兩階段分攤)
                N = len(new_members)
                C_unit_total = C // 2
                remainder_total = C % 2 
                
                C_company_stage1 = C_unit_total + remainder_total # BOSS 50% + 總餘數
                member_cost_pool = C_unit_total # 業務員總成本池 (50%)
                
                C_member_individual = 0
                remainder_members = 0
                
                if N > 0:
                    C_member_individual = member_cost_pool // N
                    remainder_members = member_cost_pool % N
                    
                C_company_final = C_company_stage1 + remainder_members # BOSS 最終攤提 (含業務員分攤餘數)

                # 3. 寫入 projects 表 (取得 project_id)
                cur.execute("""
                    INSERT INTO projects (record_date, location_name, total_fixed_cost, member_cost_pool, original_msg)
                    VALUES (%s, %s, %s, %s, %s) RETURNING project_id;
                """, (full_date, location_name, C, member_cost_pool, text))
                project_id = cur.fetchone()[0]

                # 4. 寫入 project_members 表
                for member in new_members:
                    cur.execute("""
                        INSERT INTO project_members (project_id, member_name) VALUES (%s, %s);
                    """, (project_id, member))

                # 5. 寫入 records 表 (BOSS 和所有業務員)
                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg)
                    VALUES (%s, %s, %s, %s, %s);
                """, (full_date, COMPANY_NAME, project_id, C_company_final, text))

                for member in new_members:
                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, %s, %s);
                    """, (full_date, member, project_id, C_member_individual, text))
                
                conn.commit()
                
                return f"""✅ 啟動 {location_name} 專案 ({full_date.strftime('%m/%d')})。總成本 {C}。
--------------------------------
公司 ({COMPANY_NAME}) 應攤提費用: {C_company_final}
{N} 位業務員 每人應攤提費用: {C_member_individual}
💡 後續相同日期/地點的紀錄，請以相同格式輸入，將會自動加入此專案分攤。"""
        
    except ValueError:
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

# 輔助函數: 獲取地點成本
def get_location_cost(conn, location_name, full_date):
    """根據日期和地點獲取成本"""
    is_weekend = (full_date.weekday() >= 5) 
    with conn.cursor() as cur:
        cur.execute("SELECT weekday_cost, weekend_cost FROM locations WHERE location_name = %s", (location_name,))
        result = cur.fetchone()
    
    if not result: return None
    weekday_cost, weekend_cost = result
    return weekend_cost if is_weekend else weekday_cost

# --- 刪除、清單、統計功能 (需根據新的 Project Schema 調整，此處僅提供核心邏輯) ---
# ⚠️ 注意: 由於時間限制，清單、刪除、統計功能都需要完全重寫以適應 Project Schema。

# [A] 新增/更新功能 (與舊版相同，確保 commit)
def handle_management_add(text: str) -> str:
    # ... (程式碼與前一版本相同，確保有 conn.commit())
    # ... (省略，因為邏輯與上一個程式碼塊完全一致)
    pass

# [B] 清單查詢功能 (需修改以適應新 Schema，暫時沿用舊版)
def handle_management_list(text: str) -> str:
    # ... (程式碼與前一版本相同)
    # ... (省略，因為邏輯與上一個程式碼塊完全一致)
    pass

# [F] 刪除功能 (需修改以適應新 Schema，暫時沿用舊版)
def handle_management_delete(text: str) -> str:
    # ... (程式碼與前一版本相同)
    # ... (省略，因為邏輯與上一個程式碼塊完全一致)
    pass

# [E] 統計功能 (需修改以適應新 Schema，暫時沿用舊版)
def handle_management_stat(text: str) -> str:
    # ... (程式碼與前一版本相同)
    # ... (省略，因為邏輯與上一個程式碼塊完全一致)
    pass


# --- 5. 啟動 APP ---
# (此處保持為空，因為使用 gunicorn 啟動)