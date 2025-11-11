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

# --- 2. 資料庫連接與初始化 (與前一版相同) ---

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
            
            if force_recreate:
                app.logger.warning("❗❗❗ 正在執行強制刪除並重建所有表格以修正 Schema。資料將遺失。❗❗❗")
                cur.execute("DROP TABLE IF EXISTS records;")
                cur.execute("DROP TABLE IF EXISTS project_members;")
                cur.execute("DROP TABLE IF EXISTS projects;") 
                cur.execute("DROP TABLE IF EXISTS monthly_settlements;") 
                cur.execute("DROP TABLE IF EXISTS monthly_items;")       
                cur.execute("DROP TABLE IF EXISTS locations;")
                cur.execute("DROP TABLE IF EXISTS members;")
            
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
            
            # 4. 月度成本項目設定表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_items (
                    item_name VARCHAR(50) PRIMARY KEY,
                    default_members TEXT NOT NULL, 
                    memo TEXT
                );
            """)
            
            # 5. 月度成本實際結算表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_settlements (
                    id SERIAL PRIMARY KEY,
                    item_name VARCHAR(50) REFERENCES monthly_items(item_name) ON DELETE RESTRICT,
                    settlement_date DATE NOT NULL, 
                    cost_amount INTEGER NOT NULL,
                    actual_members TEXT NOT NULL, 
                    original_msg TEXT,
                    UNIQUE (settlement_date, item_name) -- 確保每月同一個項目只有一筆結算
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
            
            cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (COMPANY_NAME,))
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

# --- 3. Webhook 處理 (更新指令分派 - 新增報表) ---
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
        
        # 處理管理指令
        if original_text.startswith('新增') or original_text.startswith('刪除') or \
           original_text.startswith('清單') or original_text.startswith('統計') or \
           original_text.startswith('結算') or original_text.startswith('報表'): # 🌟 新增 '報表'
            
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
            elif text.startswith('報表'): # 🌟 處理報表指令
                response = handle_report(text)
            else:
                response = "無法識別的管理指令。"

        elif original_text == '測試':
            response = "Bot 正常運作中！資料庫連接狀態良好。"
        elif record_match:
            record_text = record_match.group(1) + " " + record_match.group(2)
            response = handle_record_expense(record_text)
        else:
            response = "無法識別的指令格式。請輸入 '清單 地點' 或 '9/12(五) 人名 地點' (v6.3)。"
            
    except Exception as e:
        app.logger.error(f"處理指令失敗: {e}")
        response = f"指令處理發生未知錯誤: {e}"

    if not response:
        response = "處理過程中發生未預期的錯誤，請檢查指令格式或回報問題。"

    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=response)
    )

# --- 4. 核心功能實現 (僅保留新增的函數，其餘與 v6.2 相同) ---

# [J] 🌟 新增報表匯出功能 (純文字表格)
def handle_report(text: str) -> str:
    """
    處理報表指令。格式: 報表 [月份 (例如 11月)]
    回傳純文字表格報表，方便複製貼上至試算表。
    """
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
            # 查詢該月份所有紀錄 (活動攤提和月成本攤提)
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

            # 構建純文字表格
            report_lines = []
            
            # 報表標頭 (使用 TAB 鍵分隔，方便 Excel/試算表複製貼上)
            header = "日期\t紀錄類型\t項目/地點\t攤提人\t攤提金額\t項目總成本"
            report_lines.append(header)
            
            for row in data:
                record_date, member_name, cost_paid, item_name, record_type, total_cost_for_item = row
                
                # 確保數值有格式化
                cost_paid_str = f"{cost_paid:,}"
                total_cost_str = f"{total_cost_for_item:,}" if total_cost_for_item else ""

                line = f"{record_date.strftime('%Y/%m/%d')}\t{record_type}\t{item_name}\t{member_name}\t{cost_paid_str}\t{total_cost_str}"
                report_lines.append(line)
            
            # 加上總結
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

# [A] 新增/更新功能 (與 v6.2 相同)
def handle_management_add(text: str) -> str:
    # ... (程式碼與 v6.2 相同，請從 v6.2 複製貼上)
    parts = text.split()
    conn = get_db_connection()
    if not conn: return "❌ 資料庫連接失敗。"

    try:
        with conn.cursor() as cur:
            if len(parts) == 2 and parts[0] == '新增人名':
                member_name = parts[1]
                cur.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (member_name,))
                if cur.rowcount > 0:
                    conn.commit()
                    return f"✅ 已成功新增成員：{member_name}。"
                else:
                    return f"💡 成員 {member_name} 已存在。"
            elif len(parts) == 4 and parts[1] == '地點':
                loc_name, cost_val = parts[2], int(parts[3])
                cur.execute("""
                    INSERT INTO locations (location_name, weekday_cost, weekend_cost)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (location_name) DO UPDATE SET weekday_cost = EXCLUDED.weekday_cost, weekend_cost = EXCLUDED.weekend_cost;
                """, (loc_name, cost_val, cost_val))
                conn.commit()
                return f"✅ 地點「{loc_name}」已設定成功，平日/假日成本皆為 {cost_val}。"
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
                return "❌ 新增 地點/人名 指令格式錯誤。\n新增人名 [人名]\n新增 地點 [地點名] [成本](單一)\n新增 地點 [地點名] [平日成本] [假日成本](雙費率)"

    except ValueError:
        return "❌ 成本金額必須是數字。"
    except Exception as e:
        conn.rollback()
        app.logger.error(f"新增指令資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()

# [H] 新增月度成本項目設定 (與 v6.2 相同)
def handle_management_add_monthly_item(text: str) -> str:
    # ... (程式碼與 v6.2 相同，請從 v6.2 複製貼上)
    parts = text.split()
    
    if len(parts) < 4 or parts[0] != '新增' or parts[1] != '月項目':
        return "❌ 新增月項目格式錯誤。請使用: 新增 月項目 [項目名] [人名1] [人名2]..."

    item_name = parts[2]
    member_names = parts[3:]
    memo = f"月度固定成本：{item_name}"
    
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
預設分攤人 (含 {COMPANY_NAME}): {member_list_str.replace(',', '、')}"""

    except Exception as e:
        conn.rollback()
        app.logger.error(f"新增月項目資料庫錯誤: {e}")
        return f"❌ 資料庫操作失敗: {e}"
    finally:
        if conn: conn.close()
        
# [I] 新增月度成本實際結算 (與 v6.2 相同)
def handle_settle_monthly_cost(text: str) -> str:
    # ... (程式碼與 v6.2 相同，請從 v6.2 複製貼上)
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
            cur.execute("SELECT default_members FROM monthly_items WHERE item_name = %s;", (item_name,))
            item_data = cur.fetchone()
            if not item_data:
                return f"❌ 找不到月成本項目「{item_name}」。請先使用 '新增 月項目' 設定。"
            
            default_members = item_data[0].split(',') if item_data[0] else []
            
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

            all_sharers = final_members + [COMPANY_NAME]
            total_sharers = len(all_sharers)
            
            cost_per_sharer = cost_amount // total_sharers
            remainder = cost_amount % total_sharers
            
            company_cost = cost_per_sharer + remainder
            
            cur.execute("SELECT id FROM monthly_settlements WHERE settlement_date = %s AND item_name = %s;", 
                        (settlement_date, item_name))
            old_settlement_id_data = cur.fetchone()
            
            if old_settlement_id_data:
                cur.execute("DELETE FROM monthly_settlements WHERE id = %s;", (old_settlement_id_data[0],))

            actual_members_str = ','.join(final_members)
            cur.execute("""
                INSERT INTO monthly_settlements (item_name, settlement_date, cost_amount, actual_members, original_msg)
                VALUES (%s, %s, %s, %s, %s) RETURNING id;
            """, (item_name, settlement_date, cost_amount, actual_members_str, text))
            monthly_settlement_id = cur.fetchone()[0]

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
實際成本: {cost_amount:,} 元
實際分攤人 (共 {total_sharers} 位): {member_list_display}、{COMPANY_NAME}
每位業務員攤提: {cost_per_sharer} 元
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
        
# [B] 清單查詢功能 (與 v6.2 相同)
def handle_management_list(text: str) -> str:
    # ... (程式碼與 v6.2 相同，請從 v6.2 複製貼上)
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

            elif list_type == '月項目':
                cur.execute("SELECT item_name, default_members FROM monthly_items ORDER BY item_name;")
                monthly_items = cur.fetchall()
                if not monthly_items: return "📋 目前沒有任何已設定的月度成本項目。"
                response = "📋 **現有月度成本項目 (預設分攤):**\n"
                for item_name, default_members in monthly_items:
                    members = default_members.replace(',', '、')
                    response += f"• {item_name}: (預設人: {members}、{COMPANY_NAME})\n"
                return response.strip()

            elif list_type == '月結算':
                cur.execute("""
                    SELECT s.settlement_date, s.item_name, s.cost_amount, s.actual_members 
                    FROM monthly_settlements s 
                    ORDER BY s.settlement_date DESC, s.item_name;
                """)
                monthly_settlements = cur.fetchall()
                if not monthly_settlements: return "📋 目前沒有任何月度成本結算紀錄。"
                response = "📋 **現有月度成本結算紀錄:**\n"
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
        
# [E] 費用統計功能 (與 v6.2 相同)
def handle_management_stat(text: str) -> str:
    # ... (程式碼與 v6.2 相同，請從 v6.2 複製貼上)
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
        
# [F] 刪除功能 (與 v6.2 相同)
def handle_management_delete(text: str) -> str:
    # ... (程式碼與 v6.2 相同，請從 v6.2 複製貼上)
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