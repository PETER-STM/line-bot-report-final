import os
import databases
import sqlalchemy
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy.dialects.postgresql import UUID
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# --- 1. 資料庫連線與配置 ---

# 從環境變數獲取 DATABASE_URL (Railway 會自動注入)
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set. Please check Railway setup.")

database = databases.Database(DATABASE_URL)
metadata = sqlalchemy.MetaData()

# 定義 users 表格結構 - 包含所有修復和欄位名稱匹配
users = sqlalchemy.Table(
    "users",
    metadata,
    # 修正 1: id 欄位 (UUID 主鍵，設定 server_default 自動生成)
    sqlalchemy.Column("id", 
                      UUID(as_uuid=True), 
                      primary_key=True,
                      server_default=sqlalchemy.text("gen_random_uuid()"),
                      nullable=False),
    
    # 修正 2: LINE ID 欄位名稱和索引
    sqlalchemy.Column("line_id", sqlalchemy.String(50), unique=True, index=True, nullable=False), 
    
    # 修正 3: 使用者名稱欄位名稱
    sqlalchemy.Column("user_name", sqlalchemy.String(100), nullable=False),
    
    # 其他可選欄位
    sqlalchemy.Column("pictureUrl", sqlalchemy.String(255), nullable=True),
    sqlalchemy.Column("statusMessage", sqlalchemy.String(255), nullable=True),
    
    # 修正 4 & 5: 時間欄位 (設定 server_default 自動填寫，解決 NOT NULL 錯誤)
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, server_default=sqlalchemy.func.now(), nullable=False),
    sqlalchemy.Column("updated_at", sqlalchemy.DateTime, server_default=sqlalchemy.func.now(), onupdate=sqlalchemy.func.now(), nullable=False),
)

# 引擎 (用於執行 CREATE TABLE)
engine = sqlalchemy.create_engine(
    DATABASE_URL,
    pool_size=3,
    max_overflow=0,
)

# --- 2. 應用程式啟動與關閉事件 ---
app = FastAPI()

@app.on_event("startup")
async def startup():
    """連線資料庫並檢查/創建表格。"""
    await database.connect()
    # 確保在應用程式啟動時，如果表格不存在，則依據新的定義創建它
    metadata.create_all(engine) 
    print("Database connected and tables checked/initialized successfully.")

@app.on_event("shutdown")
async def shutdown():
    """斷開資料庫連線。"""
    await database.disconnect()
    print("Database connection closed.")

# --- 3. LINE Bot Webhook 路由 ---

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("Warning: LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET is not set.")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.post("/callback")
async def handle_callback(request: Request):
    """處理 LINE Webhook 驗證和訊息事件。"""
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        # 這是 Webhook 驗證失敗或格式錯誤，返回 400
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature header")
        
    body = await request.body()
    
    try:
        handler.handle(body.decode(), signature)
    except Exception as e:
        print(f"Error handling webhook: {e}")
        # 返回 200 OK，避免 LINE 平台重複發送
        return "OK" 
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
async def handle_message(event):
    """處理收到的文字訊息，包含查詢、新增和更新邏輯。"""
    text = event.message.text.strip()
    user_id = event.source.user_id

    # 查詢用戶資訊
    query = users.select().where(users.c.line_id == user_id)
    user_info = await database.fetch_one(query=query)
    
    reply_text = ""
    
    # --- 邏輯 A: 處理新增/更新人名指令 (例如：新增 王小明) ---
    if text.lower().startswith("新增 "):
        # 從訊息中提取用戶名 (移除 "新增 " 部分並清理空格)
        new_name = text[len("新增 "):].strip()

        if not new_name:
            reply_text = "❌ 格式錯誤。請發送「新增 [人名]」，例如：新增 王小明。"
        elif user_info:
            # 如果用戶已存在，則執行更新操作
            update_query = users.update().where(users.c.line_id == user_id).values(user_name=new_name)
            await database.execute(update_query)
            reply_text = f"✅ 您的名字已更新成功！新名字為：{new_name}。"
        else:
            # 執行 INSERT INTO 語句，註冊新用戶
            insert_query = users.insert().values(line_id=user_id, user_name=new_name)
            await database.execute(insert_query)
            reply_text = f"✅ 恭喜！您已成功註冊。您的名字為：{new_name}。"
            
    # --- 邏輯 B: 處理查詢/一般訊息 (非新增指令) ---
    else:
        if user_info:
            reply_text = f"您好，{user_info.user_name}！您的查詢結果：'{text}'"
        else:
            # 提示用戶如何註冊
            reply_text = "抱歉，您的 ID 尚未在名單中。請發送「新增 [人名]」來加入名單。"
    
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
        print(f"Replied to user {user_id}: {reply_text}")
    except Exception as e:
        print(f"Error sending reply message: {e}")