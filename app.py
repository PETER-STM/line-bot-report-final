import os
import datetime
import psycopg2
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# --- 1. 設定 & 初始化 ---
# ⚠️ 這些應從 Railway 的環境變數中設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
# Railway 會自動將連線字串設定為 DATABASE_URL
DATABASE_URL = os.environ.get('DATABASE_URL') 
# 您指定要查詢的日期 (注意格式 YYYY-MM-DD)
TARGET_DATE = "2025-11-09" 

# 檢查必要的環境變數
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not DATABASE_URL:
    print("ERROR: Missing required environment variables.")
    # 在實際部署中，這裡應拋出錯誤或確保變數已設定

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
app = Flask(__name__)

# --- 2. 資料庫連線函式 ---
def get_db_connection():
    """建立並回傳 PostgreSQL 連線物件"""
    try:
        # 使用 psycopg2 連接到 PostgreSQL
        conn = psycopg2.connect(DATABASE_URL)
        # 設定 row_factory 以便能以欄位名稱存取結果 (類似字典)
        # 注意: psycopg2 預設回傳 tuple，所以後續查詢時需要調整存取方式
        return conn
    except Exception as e:
        app.logger.error(f"PostgreSQL 連線失敗: {e}")
        return None

def init_db():
    """初始化資料庫：建立 Users 和 Daily_Records 表格"""
    conn = get_db_connection()
    if not conn:
        return
        
    cursor = conn.cursor()
    
    # 建立 Users 表 (用戶名單)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Users (
            user_id TEXT PRIMARY KEY,
            name TEXT
        );
    """)
    
    # 建立 Daily_Records 表 (心得記錄)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Daily_Records (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            record_date DATE,
            content TEXT,
            FOREIGN KEY(user_id) REFERENCES Users(user_id)
        );
    """)
    
    # 範例：手動插入需要登記的人 (您需要替換成您實際的用戶名單及 LINE User ID)
    # ****************** 重要：請您替換此處的 User ID ******************
    # 假設 '伊森' 的 LINE user_id 是 'U1234567890abcdef' (這是一個範例，您需要實際取得)
    # cursor.execute("INSERT INTO Users (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING;", ('U1234567890abcdef', '伊森'))
    # cursor.execute("INSERT INTO Users (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING;", ('Ufedcba0987654321', 'Ariel'))
    # *****************************************************************
    
    conn.commit()
    cursor.close()
    conn.close()

# 啟動時執行資料庫初始化
with app.app_context():
    init_db()

# --- 3. LINE Webhook 處理 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature. Check token/secret.")
        abort(400)
    except Exception as e:
        app.logger.error(f"Webhook 處理錯誤: {e}")
        abort(500)
        
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    
    reply_text = "我只聽得懂 '心得：[內容]' 和 '查詢未登記'"
    
    conn = get_db_connection()
    if not conn:
        reply_text = "❌ 內部系統錯誤：無法連接到資料庫。"
    else:
        cursor = conn.cursor()
        
        if text.startswith('心得：'):
            # --- 處理心得登記 (自動使用今天的日期) ---
            content = text[3:].strip()
            # 取得當前日期 (格式 YYYY-MM-DD)
            today_date = datetime.date.today().strftime('%Y-%m-%d')
            
            # 檢查用戶是否已在名單中
            cursor.execute("SELECT name FROM Users WHERE user_id = %s", (user_id,))
            user_info = cursor.fetchone()

            if user_info:
                try:
                    # 嘗試插入記錄
                    cursor.execute(
                        "INSERT INTO Daily_Records (user_id, record_date, content) VALUES (%s, %s, %s);",
                        (user_id, today_date, content)
                    )
                    conn.commit()
                    reply_text = f"✅ 心得登記成功！\n📅 日期: {today_date}"
                except Exception as e:
                    # 這裡通常是重複登記的錯誤，或是其他資料庫錯誤
                    app.logger.error(f"登記失敗: {e}")
                    reply_text = f"⚠️ 登記失敗，可能是您今天 ({today_date}) 已經登記過了，或發生資料庫錯誤。"
            else:
                reply_text = "🤔 您尚未在登記名單中，請先聯繫管理員將您的 LINE ID 加入 Users 表格。"

        elif text == '查詢未登記':
            # --- 處理查詢特定日期未登記名單 ---
            query_date = TARGET_DATE
            
            # 查詢 Users 表中，user_id 不在 Daily_Records 中且日期為 query_date 的用戶
            cursor.execute("""
                SELECT name FROM Users
                WHERE user_id NOT IN (
                    SELECT user_id FROM Daily_Records WHERE record_date = %s
                )
            """, (query_date,))
            
            not_recorded_users = [row[0] for row in cursor.fetchall()] # psycopg2 預設回傳 tuple，row[0] 為 name

            if not_recorded_users:
                names = '、'.join(not_recorded_users)
                reply_text = f"📅 **{query_date}** 尚未登記心得名單：\n**{names}**"
            else:
                reply_text = f"🎉 **{query_date}** 所有人都已登記完成！"
        
        # 關閉連線
        cursor.close()
        conn.close()
            
    # 回覆訊息
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    # 本機測試 (如果要在本機執行，您需要使用外部 IP 位址連線到 Railway 的 DB，或者使用 Ngrok 等工具測試 LINE Webhook)
    print("WARNING: 本機運行時，請確保已設定環境變數，並可連線到 Railway DB。")
    # app.run(debug=True, port=8000)
    pass