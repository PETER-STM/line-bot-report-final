import os
import re
from datetime import datetime, date
from flask import Flask, request, abort
from linebot import LineBotBotApi, WebhookHandler
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
    初始化資料庫表格 (地點、成員、專案、紀錄)。
    """
    conn = get_db_connection()
    if not conn:
        return
        
    try:
        with conn.cursor() as cur:
            
            # --- 永久移除強制重建，只在明確要求時執行 ---
            if force_recreate:
                app.logger.warning("❗❗❗ 正在執行強制刪除並重建所有表格以修正 Schema。資料將遺失。❗❗❗")
                cur.execute("DROP TABLE IF EXISTS records;")
                cur.execute("DROP TABLE IF EXISTS project_members;")
                cur.execute("DROP TABLE IF EXISTS projects;") 
                cur.execute("DROP TABLE IF EXISTS locations;")
                cur.execute("DROP TABLE IF EXISTS members;")
                cur.execute("DROP TABLE IF EXISTS monthly_costs;") # 新增
            # ---------------------------------------------------
                
            # 1. 地點設定表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS locations (
                    location_name VARCHAR(50) PRIMARY KEY,
                    weekday_cost INTEGER NOT NULL,
                    weekend_cost INTEGER NOT NULL
                    -- 營業時間相關欄位可在此處新增
                );
            """)
            # 2. 成員名單表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    name VARCHAR(50) PRIMARY KEY
                );
            """)

            # 3. 專案/活動表 (Project-Based Cost)
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
            
            # 4. 月度成本分攤表 (Monthly Fixed Cost)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_costs (
                    id SERIAL PRIMARY KEY,
                    cost_date DATE NOT NULL,
                    cost_amount INTEGER NOT NULL,
                    member_list TEXT,
                    memo TEXT,
                    UNIQUE (cost_date) -- 確保每月只有一筆紀錄
                );
            """)
            
            # 5. 專案參與成員表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS project_members (
                    project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE,
                    member_name VARCHAR(50) REFERENCES members(name) ON DELETE CASCADE,
                    PRIMARY KEY (project_id, member_name)
                );
            """)

            # 6. 費用紀錄表
            # 新增 monthly_cost_id 欄位，允許其為 NULL (用於區分是 Project 紀錄還是 Monthly 紀錄)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    id SERIAL PRIMARY KEY,
                    record_date DATE NOT NULL,
                    member_name VARCHAR(50) REFERENCES members(name) ON DELETE CASCADE,
                    project_id UUID REFERENCES projects(project_id) ON DELETE CASCADE NULL,
                    monthly_cost_id INTEGER REFERENCES monthly_costs(id) ON DELETE CASCADE NULL,
                    cost_paid INTEGER NOT NULL,
                    original_msg TEXT,
                    
                    -- 確保 project_id 和 monthly_cost_id 只有一個有值
                    CONSTRAINT chk_one_id_not_null CHECK (
                        (project_id IS NOT NULL AND monthly_cost_id IS NULL) OR 
                        (project_id IS NULL AND monthly_cost_id IS NOT NULL)
                    )
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
        conn.rollback()
        app.logger.error(f"資料庫初始化失敗: {e}")
    finally:
        if conn: conn.close()

init_db(force_recreate=False) 

# --- 3. Webhook 處理 (已新增月成本指令處理) ---
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
    original_text = event.message.text.strip()
    reply_token = event.reply_token
    response = ""

    try:
        record_match = re.search(r'(\d{1,2}/\d{1,2}[\(\（]\w[\)\）])\s+([^\s]+.*)', original_text)
        
        # 處理管理指令 (新增月成本)
        if original_text.startswith('新增') or original_text.startswith('刪除') or original_text.startswith('清單') or original_text.startswith('統計') or original_text.startswith('月成本'):
            text = original_text.split('\n')[0].strip() 
            
            if text.startswith('新增'):
                response = handle_management_add(text)
            elif text.startswith('刪除'):
                response = handle_management_delete(text)
            elif text.startswith('清單'):
                response = handle_management_list(text)
            elif text.startswith('統計'):
                response = handle_management_stat(text)
            elif text.startswith('月成本'):
                response = handle_monthly_cost(text) # 🌟 新增的月成本處理函數
        elif original_text == '測試':
            response = "Bot 正常運作中！資料庫連接狀態良好。"
        elif record_match:
            record_text = record_match.group(1) + " " + record_match.group(2)
            response = handle_record_expense(record_text)
        else:
            response = "無法識別的指令格式。請輸入 '清單 地點' 或 '9/12(五) 人名 地點' (v6.1)。"
            
    except Exception as e:
        app.logger.error(f"處理指令失敗: {e}")
        response = f"指令處理發生未知錯誤: {e}"

    if not response:
        response = "處理過程中發生未預期的錯誤，請檢查指令格式或回報問題。"

    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=response)
    )

# --- 4. 核心功能實現 ---

# [C] 日期解析 (與前一版本相同)
def parse_record_command(text: str):
    """
    解析費用紀錄指令。格式: [月/日(星期)] [人名1] [地點名] [人名2]... [金額(可選)]
    """
    # 支援中/英文括號: [\(\（](\w)[\)\）]
    date_match = re.match(r'^(\d{1,2}/\d{1,2})[\(\（](\w)[\)\）]', text)
    if not date_match:
        return None, "日期格式錯誤 (月/日(星期))"

    record_date_str = date_match.group(1) 
    
    # 年份自動判斷 (邏輯略)
    today = date.today()
    current_year = today.year
    input_month = int(record_date_str.split('/')[0])
    
    record_year = current_year
    if today.month == 12 and input_month == 1 or (today.month > 1 and input_month < today.month):
        record_year = current_year + 1
    elif today.month == 1 and input_month == 12 or (today.month > 1 and input_month > today.month):
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
    
    # 🌟 雜訊過濾器
    FILTER_WORDS = ['好', '桌5布4燈1', '架1']
    parts = [p for p in remaining_text.split() if p not in FILTER_WORDS] 
    
    if len(parts) < 2:
        return None, "請至少指定一位人名和一個地點"

    # --- 順序修正：第一個是人名 (人名1)，第二個是地點名 ---
    member_names = [parts[0]] 
    location_name = parts[1]  
    
    # 後面所有詞都當作是人名
    if len(parts) > 2:
        member_names.extend(parts[2:])

    if COMPANY_NAME in member_names:
        return None, f"請勿在紀錄中包含 {COMPANY_NAME}，它會自動加入計算。"

    return {
        'full_date': full_date,
        'day_of_week': date_match.group(2), 
        'member_names': member_names,
        'location_name': location_name,
        'manual_cost': manual_cost
    }, None

# 輔助函數: 獲取地點成本 (與前一版本相同)
def get_location_cost(conn, location_name, full_date):
    """根據日期和地點獲取成本"""
    is_weekend = (full_date.weekday() >= 5) 
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT weekday_cost, weekend_cost FROM locations WHERE location_name = %s", (location_name,))
            result = cur.fetchone()
        
        if not result: return None
        weekday_cost, weekend_cost = result
        return weekend_cost if is_weekend else weekday_cost
    except Exception as e:
        app.logger.error(f"獲取地點成本失敗: {e}")
        return None

# [D] 費用紀錄功能 (Project-Based 邏輯 - 與前一版本相同)
def handle_record_expense(text: str) -> str:
    """處理費用紀錄指令，實作 Project-Based 兩階段分攤邏輯。"""
    parsed_data, error = parse_record_command(text)
    if error:
        return f"❌ 指令解析失敗: {error}"
        
    full_date = parsed_data['full_date']
    new_members = parsed_data['member_names'] 
    location_name = parsed_data['location_name']
    manual_cost = parsed_data['manual_cost']

    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            # ... (專案存在/不存在邏輯，與前一版本完全相同)
            
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
                
                cur.execute("""
                    SELECT member_name FROM project_members WHERE project_id = %s;
                """, (project_id,))
                current_members = [row[0] for row in cur.fetchall()]
                
                members_to_add = [m for m in new_members if m not in current_members]
                
                if not members_to_add and len(new_members) > 0:
                    return f"💡 {location_name} 在 {full_date.strftime('%m/%d')} 的紀錄已存在，且所有指定成員都已加入分攤名單。"

                all_business_members = sorted(list(set(current_members) | set(new_members)))
                
                N = len(all_business_members)
                C_member_individual = 0
                remainder_members = 0

                if N > 0:
                    C_member_individual = member_cost_pool // N
                    remainder_members = member_cost_pool % N

                C_company_final = member_cost_pool + remainder_members
                
                for member in members_to_add:
                    cur.execute("""
                        INSERT INTO project_members (project_id, member_name) VALUES (%s, %s) 
                        ON CONFLICT (project_id, member_name) DO NOTHING;
                    """, (project_id, member))

                cur.execute("DELETE FROM records WHERE project_id = %s;", (project_id,))
                
                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, monthly_cost_id, cost_paid, original_msg)
                    VALUES (%s, %s, %s, NULL, %s, %s);
                """, (full_date, COMPANY_NAME, project_id, C_company_final, text))

                for member in all_business_members:
                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, monthly_cost_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, NULL, %s, %s);
                    """, (full_date, member, project_id, C_member_individual, text))
                
                conn.commit()
                return f"""✅ 成功加入新成員至 {location_name} ({full_date.strftime('%m/%d')}) 專案。
--------------------------------
總業務員人數已更新為 {N} 位。
每位業務員應攤提費用: {C_member_individual}
{COMPANY_NAME} 應攤提費用: {C_company_final:,} (固定成本 + 餘數)"""


            # --- 情況 B: 專案不存在 (初次紀錄/Project Lead) ---
            else:
                C = manual_cost if manual_cost is not None else get_location_cost(conn, location_name, full_date)
                if C is None:
                    return f"❌ 地點 '{location_name}' 尚未設定成本，請先使用 '新增 地點' 指令。"

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

                cur.execute("""
                    INSERT INTO projects (record_date, location_name, total_fixed_cost, member_cost_pool, original_msg)
                    VALUES (%s, %s, %s, %s, %s) RETURNING project_id;
                """, (full_date, location_name, C, member_cost_pool, text))
                project_id = cur.fetchone()[0]

                for member in new_members:
                    cur.execute("""
                        INSERT INTO project_members (project_id, member_name) VALUES (%s, %s);
                    """, (project_id, member))

                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, monthly_cost_id, cost_paid, original_msg)
                    VALUES (%s, %s, %s, NULL, %s, %s);
                """, (full_date, COMPANY_NAME, project_id, C_company_final, text))

                for member in new_members:
                    cur.execute("""
                        INSERT INTO records (record_date, member_name, project_id, monthly_cost_id, cost_paid, original_msg)
                        VALUES (%s, %s, %s, NULL, %s, %s);
                    """, (full_date, member, project_id, C_member_individual, text))
                
                conn.commit()
                
                return f"""✅ 啟動 {location_name} 專案 ({full_date.strftime('%m/%d')})。總成本 {C}。
--------------------------------
公司 ({COMPANY_NAME}) 應攤提費用: {C_company_final:,}
{N} 位業務員 每人應攤提費用: {C_member_individual}
💡 後續相同日期/地點的紀錄，請以相同格式輸入，將會自動加入此專案分攤。"""
        
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
        
# [G] 🌟 新增月度固定成本功能
def handle_monthly_cost(text: str) -> str:
    """
    處理月成本攤提指令。格式: 月成本 [月份 (如 11月)] [金額] [人名1] [人名2]... [備註]
    """
    parts = text.split()
    if len(parts) < 4 or parts[0] != '月成本':
        return "❌ 月成本指令格式錯誤。請使用: 月成本 [月份 (如 11月)] [金額] [人名1] [人名2]... [備註/項目名]"
        
    month_str = parts[1].replace('月', '').strip()
    
    try:
        target_month = int(month_str)
        cost_amount = int(parts[2])
    except ValueError:
        return "❌ 月份或金額必須是有效的數字。"

    member_names_raw = parts[3:]
    
    # 提取備註 (預設為 '月度固定成本')
    memo = "月度固定成本"
    if member_names_raw:
        # 尋找第一個非人名（備註）
        potential_memo_start_index = 0
        
        # 簡易判斷：如果最後一個詞不是人名，則視為備註
        if len(member_names_raw) > 1:
            memo_parts = member_names_raw[-1].split(':')
            if len(memo_parts) > 1 and memo_parts[0] == '項目':
                 memo = memo_parts[1]
                 member_names = member_names_raw[:-1]
            else:
                member_names = member_names_raw
                memo = ' '.join(member_names_raw) # 假設所有剩餘的都是人名，備註使用預設
        else:
            member_names = member_names_raw
    else:
        return "❌ 請至少指定一位分攤人名。"
        
    
    # 假設指令格式為: 月成本 11月 10000 人名1 人名2 項目:租金
    # 讓我們重新調整人名和備註的提取邏輯
    
    # 提取所有在人名前面的詞彙
    name_and_memo = parts[3:]
    member_names = []
    memo = "月度固定成本"
    
    # 找出備註（假設備註是最後一個詞，且可能含有中文）
    if name_and_memo:
        # 嘗試在最後一個詞尋找備註分隔符號
        last_part = name_and_memo[-1]
        if ":" in last_part or "：" in last_part:
            memo = last_part.replace('項目:', '').replace('項目：', '')
            member_names = name_and_memo[:-1]
        else:
             # 如果沒有分隔符號，則嘗試從第一個非數字/非單一人名的詞開始
            member_names = name_and_memo
            memo = "月度固定成本" # 避免將人名誤判為備註
            
    # 如果人名清單為空
    if not member_names:
        return "❌ 請至少指定一位分攤人名。"
    
    # 移除 COMPANY_NAME (會自動加入)
    member_names = [n for n in member_names if n != COMPANY_NAME]
    
    # 計算日期 (取當年的目標月份的第一天)
    current_year = date.today().year
    
    # 如果目標月份小於當前月份，則假定為明年 (例如 12月問 1月)
    if target_month < date.today().month and date.today().month == 12:
         current_year += 1
    
    try:
        cost_date = date(current_year, target_month, 1)
    except ValueError:
        return "❌ 無效的月份或年份計算錯誤。"

    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            # 檢查所有指定人名是否存在
            for name in member_names:
                cur.execute("SELECT name FROM members WHERE name = %s", (name,))
                if cur.fetchone() is None:
                    return f"❌ 成員 {name} 不存在。請先使用 '新增人名'。"

            # 總分攤人數 (所有業務員 + 公司)
            total_sharers = len(member_names) + 1 
            
            # 計算平均分攤金額和餘數
            cost_per_sharer = cost_amount // total_sharers
            remainder = cost_amount % total_sharers
            
            # 公司的最終分攤金額 (業務員分攤金額 + 餘數)
            company_cost = cost_per_sharer + remainder
            
            # 檢查該月份是否已記錄，如果已記錄，則刪除舊的 (更新)
            cur.execute("SELECT id FROM monthly_costs WHERE cost_date = %s;", (cost_date,))
            old_cost_id = cur.fetchone()
            
            if old_cost_id:
                # 級聯刪除舊的 records
                cur.execute("DELETE FROM monthly_costs WHERE id = %s;", (old_cost_id[0],))
            
            # 1. 寫入 monthly_costs 表 (取得 ID)
            member_list_str = ','.join(member_names)
            cur.execute("""
                INSERT INTO monthly_costs (cost_date, cost_amount, member_list, memo)
                VALUES (%s, %s, %s, %s) RETURNING id;
            """, (cost_date, cost_amount, member_list_str, memo))
            monthly_cost_id = cur.fetchone()[0]

            # 2. 寫入 records 表 (公司)
            cur.execute("""
                INSERT INTO records (record_date, member_name, project_id, monthly_cost_id, cost_paid, original_msg)
                VALUES (%s, %s, NULL, %s, %s, %s);
            """, (cost_date, COMPANY_NAME, monthly_cost_id, company_cost, text))

            # 3. 寫入 records 表 (業務員)
            for member in member_names:
                cur.execute("""
                    INSERT INTO records (record_date, member_name, project_id, monthly_cost_id, cost_paid, original_msg)
                    VALUES (%s, %s, NULL, %s, %s, %s);
                """, (cost_date, member, monthly_cost_id, cost_per_sharer, text))
            
            conn.commit()
            
            action = "更新" if old_cost_id else "新增"
            return f"""✅ 成功{action} {target_month} 月份月成本分攤：『{memo}』
--------------------------------
總成本: {cost_amount:,} 元
總分攤人數: {total_sharers} (包含 {COMPANY_NAME})
每位業務員攤提: {cost_per_sharer} 元
{COMPANY_NAME} 攤提: {company_cost:,} 元 (含餘數 {remainder})"""
        
    except Exception as e:
        conn.rollback()
        app.logger.error(f"月成本指令資料庫錯誤: {e}")
        return f"❌ 處理月成本發生錯誤: {e}"
    finally:
        if conn: conn.close()
        
# [A] 新增/更新功能 (與前一版本相同)
def handle_management_add(text: str) -> str:
    # ... (程式碼與前一版本完全相同)
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
                    INSERT INTO locations (location_name, weekday_cost, weekend_cost)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (location_name) DO UPDATE SET weekday_cost = EXCLUDED.weekday_cost, weekend_cost = EXCLUDED.weekend_cost;
                """, (loc_name, cost_val, cost_val))
                conn.commit()
                return f"✅ 地點「{loc_name}」已設定成功，平日/假日成本皆為 {cost_val}。"

            # 處理：新增 地點 [地點名] [平日成本] [假日成本] (雙費率，共 5 部分)
            elif len(parts) == 5 and parts[1] == '地點':
                loc_name = parts[2]
                weekday_cost_val = int(parts[3])
                weekend_cost_val = int(parts[4])
                
                cur.execute("""
                    INSERT INTO locations (location_name, weekday_cost, weekend_cost)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (location_name) DO UPDATE SET weekday_cost = EXCLUDED.weekday_cost, weekend_cost = EXCLUDED.weekend_cost;
                """, (loc_name, weekday_cost_val, weekend_cost_val))
                conn.commit()
                return f"✅ 地點「{loc_name}」已設定成功，平日 {weekday_cost_val}，假日 {weekend_cost_val}。"
                
            else:
                return "❌ 新增指令格式錯誤。\n新增人名 [人名]\n新增 地點 [地點名] [成本](單一)\n新增 地點 [地點名] [平日成本] [假日成本](雙費率)"

    except ValueError:
        return "❌ 成本金額必須是數字。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"新增指令資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()
        
# [B] 清單查詢功能 (已新增月成本清單邏輯)
def handle_management_list(text: str) -> str:
    """處理 清單 人名/地點/月成本 指令，查詢並列出設定"""
    parts = text.split()
    if len(parts) != 2 or parts[0] != '清單':
        return "❌ 清單指令格式錯誤。請使用: 清單 人名, 清單 地點, 或 清單 月成本。"
        
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
                cur.execute("SELECT location_name, weekday_cost, weekend_cost FROM locations ORDER BY location_name;")
                locations = cur.fetchall()
                
                if not locations: return "📋 目前沒有任何已設定的地點。"

                response = "📋 **現有地點及其成本:**\n"
                for name, weekday_cost, weekend_cost in locations:
                    if weekday_cost == weekend_cost:
                        response += f"• {name}: {weekday_cost} (單一費率)\n"
                    else:
                        response += f"• {name}: 平日 {weekday_cost} / 假日 {weekend_cost}\n"
                return response.strip()

            elif list_type == '月成本': # 🌟 新增月成本清單
                cur.execute("SELECT cost_date, cost_amount, member_list, memo FROM monthly_costs ORDER BY cost_date DESC;")
                monthly_costs = cur.fetchall()
                
                if not monthly_costs: return "📋 目前沒有任何已設定的月度成本紀錄。"

                response = "📋 **現有月度成本紀錄:**\n"
                for cost_date, cost_amount, member_list, memo in monthly_costs:
                    members = member_list.replace(',', '、')
                    response += f"• {cost_date.strftime('%Y/%m')} [{memo}]: {cost_amount:,} 元 (分攤人: {members})\n"
                return response.strip()
                
            else:
                return "❌ 無法識別的清單類別。請輸入 '清單 人名', '清單 地點', 或 '清單 月成本'。"

    except Exception as e:
        app.logger.error(f"清單指令資料庫錯誤: {e}")
        return f"❌ 查詢清單發生錯誤: {e}"
    finally:
        if conn: conn.close()
        
# [E] 費用統計功能 (已更新以包含月成本)
def handle_management_stat(text: str) -> str:
    """處理 統計 [人名/公司] [月份] 指令"""
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
            # 檢查人名是否存在
            cur.execute("SELECT name FROM members WHERE name = %s", (target_name,))
            if cur.fetchone() is None:
                return f"❌ 無法統計。成員 {target_name} 不存在於名單中。"

            # 查詢特定成員在特定月份的總費用 (包含 projects 和 monthly_costs 兩種紀錄)
            cur.execute("""
                SELECT SUM(cost_paid)
                FROM records r
                WHERE r.member_name = %s 
                  AND date_part('month', r.record_date) = %s;
            """, (target_name, target_month))
            
            total_cost = cur.fetchone()[0]
            
            if total_cost is None:
                return f"✅ {target_name} 在 {target_month} 月份沒有任何費用紀錄。"
            
            # 使用千位數分隔符號讓數字更易讀
            return f"📈 **{target_name} {target_month} 月份總費用統計**：\n總攤提費用為：**{total_cost:,}** 元 (含月度成本攤提)。"

    except Exception as e:
        app.logger.error(f"統計指令資料庫錯誤: {e}")
        return f"❌ 查詢統計數據發生錯誤: {e}"
    finally:
        if conn: conn.close()
        
# [F] 刪除功能 (已更新以支援刪除月成本)
def handle_management_delete(text: str) -> str:
    """處理 刪除 地點/人名/紀錄/月成本 指令"""
    parts = text.split()
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"
    
    try:
        with conn.cursor() as cur:
            # --- 1. 刪除紀錄 (刪除 紀錄 月/日(星期) 地點名) ---
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

            # --- 2. 刪除月成本 (刪除 月成本 [月份]) --- 🌟 新增
            elif len(parts) == 3 and parts[1] == '月成本':
                month_str = parts[2].replace('月', '').strip()
                try:
                    target_month = int(month_str)
                except ValueError:
                    return "❌ 月份格式錯誤，請輸入有效的數字月份 (如 11月)。"

                current_year = date.today().year
                if target_month < date.today().month and date.today().month == 12:
                    current_year += 1
                try:
                    cost_date = date(current_year, target_month, 1)
                except ValueError:
                    return "❌ 無效的月份或年份計算錯誤。"

                cur.execute("DELETE FROM monthly_costs WHERE cost_date = %s RETURNING id;", (cost_date,))
                
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 已成功刪除 {target_month} 月份的月度固定成本紀錄。"
                else:
                    return f"💡 找不到 {target_month} 月份的月度固定成本紀錄。"


            # --- 3. 刪除成員 (刪除 人名 彼) ---
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

            # --- 4. 刪除地點 (刪除 地點 市集) ---
            elif len(parts) == 3 and parts[1] == '地點':
                loc_name = parts[2]
                cur.execute("DELETE FROM locations WHERE location_name = %s;", (loc_name,))
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 地點 {loc_name} 已成功刪除。"
                else:
                    return f"💡 地點 {loc_name} 不存在。"
                    
            else:
                return "❌ 刪除指令格式錯誤。\n刪除 人名 [人名]\n刪除 地點 [地點名]\n刪除 紀錄 [月/日(星期)] [地點名]\n刪除 月成本 [月份(如 11月)]"

    except psycopg2.errors.RestrictViolation:
        conn.rollback()
        return "❌ 地點刪除失敗: 仍有專案紀錄引用此地點。請先刪除相關的 '紀錄'。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"刪除指令資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()


# --- 5. 啟動 APP ---
# (此處保持為空，因為使用 gunicorn 啟動)