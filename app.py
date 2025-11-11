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
COMPANY_NAME = os.getenv('COMPANY_NAME', 'BOSS') # 假設您使用 BOSS 作為公司名

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
    :param force_recreate: 如果為 True，將會 DROP TABLE 並重建，以強制修正 Schema。
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
            # 3. 費用紀錄表
            cur.execute("""
                CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; 
                CREATE TABLE IF NOT EXISTS records (
                    id SERIAL PRIMARY KEY,
                    record_date DATE NOT NULL,
                    member_name VARCHAR(50) REFERENCES members(name),
                    location_name VARCHAR(50) REFERENCES locations(location_name),
                    cost_paid INTEGER NOT NULL,
                    original_msg TEXT,
                    unique_group_id UUID DEFAULT uuid_generate_v4()
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

# 啟動時自動初始化資料庫 (第一次部署時應設為 True，之後改回 False 或不帶參數)
init_db() 

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

# --- 4. 訊息處理邏輯 (路由) ---

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

# --- 5. 核心功能實現 ---

# [A] 新增/更新功能
def handle_management_add(text: str) -> str:
    """處理 新增 地點/人名 指令"""
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
                    conn.commit() # <--- 關鍵修復：新增後立即提交
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
                conn.commit() # <--- 關鍵修復：新增後立即提交
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
                conn.commit() # <--- 關鍵修復：新增後立即提交
                return f"✅ 地點「{loc_name}」已設定成功，平日 {weekday_cost_val}，假日 {weekend_cost_val}。"
                
            else:
                return "❌ 新增指令格式錯誤。\n新增人名 [人名]\n新增 地點 [地點名] [成本](單一)\n新增 地點 [地點名] [平日成本] [假日成本](雙費率)"

        # 這裡的 commit 已無必要，因為前面已處理
        # conn.commit() 
    except ValueError:
        return "❌ 成本金額必須是數字。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"新增指令資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()
        
    return "❌ 新增指令格式錯誤。"

# [B] 清單查詢功能
def handle_management_list(text: str) -> str:
    """處理 清單 人名/地點 指令，查詢並列出設定"""
    parts = text.split()
    if len(parts) != 2 or parts[0] != '清單':
        return "❌ 清單指令格式錯誤。請使用: 清單 人名 或 清單 地點。"
        
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

            else:
                return "❌ 無法識別的清單類別。請輸入 '清單 人名' 或 '清單 地點'。"

    except Exception as e:
        app.logger.error(f"清單指令資料庫錯誤: {e}")
        return f"❌ 查詢清單發生錯誤: {e}"
    finally:
        if conn: conn.close()

# [C] 日期解析 (優化版)
def parse_record_command(text: str):
    """
    解析費用紀錄指令，並自動判斷年份 (假設紀錄是發生在過去 12 個月內)。
    """
    date_match = re.match(r'^(\d{1,2}/\d{1,2})\((\w)\)', text)
    if not date_match:
        return None, "日期格式錯誤 (月/日(星期))"

    record_date_str = date_match.group(1) 
    
    # --- 年份自動判斷優化 ---
    today = date.today()
    current_year = today.year
    input_month = int(record_date_str.split('/')[0])
    
    # 判斷是否跨年 (例如今天 1 月，輸入 12 月的日期)
    if today.month == 1 and input_month == 12:
        record_year = current_year - 1
    # 判斷是否為前一年同月份之後的日期
    elif today.month > 1 and input_month > today.month:
        record_year = current_year - 1
    else:
        record_year = current_year
    
    try:
        full_date = datetime.strptime(f'{record_year}/{record_date_str}', '%Y/%m/%d').date()
    except ValueError:
        return None, "日期不存在 (例如 2月30日)"
    
    # ---------------------------

    remaining_text = text[date_match.end():].strip() 
    
    manual_cost = None
    cost_match = re.search(r'\s(\d+)$', remaining_text)
    if cost_match:
        manual_cost = int(cost_match.group(1))
        remaining_text = remaining_text[:cost_match.start()].strip() 
    
    parts = remaining_text.split()
    if len(parts) < 2:
        return None, "請至少指定一位人名和一個地點"

    location_name = parts[-1]
    member_names = parts[:-1]
    
    if COMPANY_NAME in member_names:
        return None, f"請勿在紀錄中包含 {COMPANY_NAME}，它會自動加入計算。"

    return {
        'full_date': full_date,
        'day_of_week': date_match.group(2), 
        'member_names': member_names,
        'location_name': location_name,
        'manual_cost': manual_cost
    }, None

# [D] 費用紀錄功能 (兩階段分攤邏輯)
def handle_record_expense(text: str) -> str:
    """處理費用紀錄指令，實作兩階段分攤邏輯。"""
    parsed_data, error = parse_record_command(text)
    if error:
        return f"❌ 指令解析失敗: {error}"
        
    full_date = parsed_data['full_date']
    member_names = parsed_data['member_names']
    location_name = parsed_data['location_name']
    manual_cost = parsed_data['manual_cost']
    
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        # --- 1. 取得總成本 C ---
        C = 0
        if manual_cost is not None:
            C = manual_cost
        else:
            is_weekend = (full_date.weekday() >= 5) 
            with conn.cursor() as cur:
                cur.execute("SELECT weekday_cost, weekend_cost FROM locations WHERE location_name = %s", (location_name,))
                result = cur.fetchone()
            
            if not result:
                return f"❌ 地點 '{location_name}' 尚未設定成本，請先使用 '新增 地點' 指令。"
            
            weekday_cost, weekend_cost = result
            C = weekend_cost if is_weekend else weekday_cost
            
        # --- 2. 核心計算邏輯 (兩階段分攤) ---
        N = len(member_names) 
        
        # 階段 1: 總成本 C 由 公司 (1) 和 業務員總體 (1) 平分
        C_unit_total = C // 2
        remainder_total = C % 2 
        
        C_company_stage1 = C_unit_total + remainder_total
        C_members_total = C_unit_total 
        
        C_member_individual = 0
        remainder_members = 0
        
        if N > 0:
            # 階段 2: 業務員總體成本 C_members_total 由 N 個業務員分攤
            C_member_individual = C_members_total // N
            remainder_members = C_members_total % N 
            
        # 最終公司金額 (需加上業務員分攤的餘數)
        C_company_final = C_company_stage1 + remainder_members
        
        # --- 3. 寫入紀錄 (records 表) ---
        
        with conn.cursor() as cur:
            # 寫入公司的紀錄 (優先取得 group_id)
            cur.execute("""
                INSERT INTO records (record_date, member_name, location_name, cost_paid, original_msg)
                VALUES (%s, %s, %s, %s, %s) RETURNING unique_group_id;
            """, (
                full_date, 
                COMPANY_NAME,
                location_name,
                C_company_final,
                text
            ))
            group_id = cur.fetchone()[0]

            # 寫入每個業務員的紀錄
            for member in member_names:
                cur.execute("""
                    INSERT INTO records (record_date, member_name, location_name, cost_paid, original_msg, unique_group_id)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """, (
                    full_date,
                    member,
                    location_name,
                    C_member_individual,
                    text,
                    group_id
                ))
            
        conn.commit() # <--- 關鍵修復：確保所有寫入後，提交！
        
        return f"""✅ 紀錄成功 (v3-final)！總成本 {C}。
--------------------------------
公司 ({COMPANY_NAME}) 應攤提費用: {C_company_final}
{N} 位業務員 每人應攤提費用: {C_member_individual}"""
        
    except ValueError:
        return "❌ 金額格式錯誤。"
    except psycopg2.errors.ForeignKeyViolation as fke:
        conn.rollback()
        # 檢查是人名還是地點導致的外鍵錯誤
        if 'members' in str(fke):
             return f"❌ 紀錄失敗：人名 {member_names} 尚未加入清單。請先使用 '新增人名'。"
        else: # locations
             return f"❌ 紀錄失敗：地點 {location_name} 尚未設定。請先使用 '新增 地點'。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"費用紀錄資料庫錯誤: {e}")
        return f"❌ 處理費用紀錄發生錯誤: {e}"
    finally:
        if conn: conn.close()


# [E] 費用統計功能
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

            # 查詢特定成員在特定月份的總費用
            cur.execute("""
                SELECT SUM(cost_paid)
                FROM records
                WHERE member_name = %s 
                  AND date_part('month', record_date) = %s;
            """, (target_name, target_month))
            
            total_cost = cur.fetchone()[0]
            
            if total_cost is None:
                return f"✅ {target_name} 在 {target_month} 月份沒有任何費用紀錄。"
            
            # 使用千位數分隔符號讓數字更易讀
            return f"📈 **{target_name} {target_month} 月份總費用統計**：\n總通路費用為：**{total_cost:,}** 元。"

    except Exception as e:
        app.logger.error(f"統計指令資料庫錯誤: {e}")
        return f"❌ 查詢統計數據發生錯誤: {e}"
    finally:
        if conn: conn.close()
        
# [F] 刪除功能
def handle_management_delete(text: str) -> str:
    """處理 刪除 地點/人名/紀錄 指令"""
    parts = text.split()
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"
    
    try:
        with conn.cursor() as cur:
            # --- 1. 刪除紀錄 (刪除 紀錄 月/日(星期) 人名) ---
            if len(parts) == 4 and parts[1] == '紀錄':
                date_part_str = parts[2]
                member_name = parts[3]
                
                temp_text = f"{date_part_str} {member_name} 測試地點 1"
                parsed_date_data, _ = parse_record_command(temp_text)
                
                if not parsed_date_data:
                    return "❌ 刪除紀錄指令的日期格式或內容無效 (月/日(星期))。"
                        
                record_date = parsed_date_data['full_date']

                # A. 查詢目標紀錄的 unique_group_id
                cur.execute("""
                    SELECT unique_group_id FROM records
                    WHERE record_date = %s AND member_name = %s
                    LIMIT 1;
                """, (record_date, member_name))
                
                group_id_result = cur.fetchone()

                if not group_id_result:
                    return f"💡 找不到 {member_name} 在 {date_part_str} 的費用紀錄，可能已被刪除或不存在。"

                group_id = group_id_result[0]

                # B. 使用 group_id 刪除同組所有紀錄 (包括公司攤提)
                cur.execute("DELETE FROM records WHERE unique_group_id = %s;", (group_id,))
                
                conn.commit() # <--- 關鍵修復：刪除後立即提交
                return f"✅ 已成功刪除 {member_name} 在 {date_part_str} 的紀錄。共刪除 {cur.rowcount} 筆同組紀錄 (含公司攤提)。"

            # --- 2. 刪除成員 (刪除 人名 彼) ---
            elif len(parts) == 3 and parts[1] == '人名':
                member_name = parts[2]
                if member_name == COMPANY_NAME:
                    return f"❌ 無法刪除系統專用成員 {COMPANY_NAME}。"
                    
                cur.execute("DELETE FROM members WHERE name = %s;", (member_name,))
                if cur.rowcount > 0:
                    conn.commit() # <--- 關鍵修復：刪除後立即提交
                    return f"✅ 成員 {member_name} 已從名單中刪除。但歷史費用紀錄將保留。"
                else:
                    return f"💡 名單中找不到 {member_name}。"

            # --- 3. 刪除地點 (刪除 地點 市集) ---
            elif len(parts) == 3 and parts[1] == '地點':
                loc_name = parts[2]
                cur.execute("DELETE FROM locations WHERE location_name = %s;", (loc_name,))
                if cur.rowcount > 0:
                    conn.commit() # <--- 關鍵修復：刪除後立即提交
                    return f"✅ 地點 {loc_name} 已成功刪除。"
                else:
                    return f"💡 地點 {loc_name} 不存在。"
                    
            else:
                return "❌ 刪除指令格式錯誤。\n刪除 人名 [人名]\n刪除 地點 [地點名]\n刪除 紀錄 [月/日(星期)] [人名]"

        # 這裡的 commit 已無必要
        # conn.commit() 
    except Exception as e:
        conn.rollback()
        app.logger.error(f"刪除指令資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()


# --- 6. 啟動 APP ---
# 由於使用 gunicorn 啟動，這裡的 app.run() 區塊應保持註釋或移除。
# 如果不移除，gunicorn 執行時不會執行它。
# if __name__ == "__main__":
#     port = int(os.environ.get("PORT", 5000))
#     app.run(host='0.0.0.0', port=port)