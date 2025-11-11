# db_manager.py

import os
import psycopg2
# 引入 DictCursor 以便查詢結果能以字典 (欄位名稱) 方式存取
from psycopg2.extras import DictCursor 

# --- 設定 ---
# Railway 會自動設置 DATABASE_URL
DATABASE_URL = os.getenv('DATABASE_URL') 

# --- 資料庫連線核心函數 ---

def get_db_connection():
    """建立並返回資料庫連線"""
    if not DATABASE_URL:
        print("錯誤：DATABASE_URL 環境變數未設定。")
        return None
        
    try:
        # 連接到 PostgreSQL
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except psycopg2.Error as e:
        print(f"資料庫連線失敗: {e}")
        return None

def initialize_db():
    """初始化資料庫表格：Locations, Members, Records (只需執行一次)"""
    conn = get_db_connection()
    if not conn: 
        print("初始化失敗：無法連線到資料庫。")
        return False
    
    try:
        with conn.cursor() as cur:
            # 1. Locations (地點成本表)
            cur.execute("""
               CREATE TABLE IF NOT EXISTS Locations (
    name TEXT PRIMARY KEY,  -- <-- 正確的欄位名稱是 'name'
    weekday_cost REAL NOT NULL,
    holiday_cost REAL NOT NULL
);
            """)
            
            # 2. Members (業務成員表) - name 唯一性確保不會重複新增
            cur.execute("""
                CREATE TABLE IF NOT EXISTS Members (
                    name TEXT PRIMARY KEY,
                    line_user_id TEXT
                );
            """)
            
            # 3. Records (費用紀錄表) - 儲存每次活動的分攤結果
            cur.execute("""
                CREATE TABLE IF NOT EXISTS Records (
                    id SERIAL PRIMARY KEY,
                    date_str TEXT NOT NULL,
                    member_names TEXT NOT NULL, -- 逗號分隔的人名 (e.g., '彼,明')
                    location_name TEXT NOT NULL,
                    total_cost REAL NOT NULL,
                    company_share REAL NOT NULL,
                    member_share REAL NOT NULL, -- 每位業務應付的金額
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
            print("✅ 資料庫表格初始化完成。")
            return True
            
    except psycopg2.Error as e:
        print(f"資料庫初始化失敗: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def db_execute(query, params=None):
    """執行 INSERT, UPDATE, DELETE 操作"""
    conn = get_db_connection()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
        conn.commit()
        return True
    except psycopg2.Error as e:
        print(f"DB 執行錯誤: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def db_query(query, params=None, fetch_one=True):
    """執行 SELECT 查詢操作，使用 DictCursor 返回字典格式結果"""
    # 使用 DictCursor 可以讓結果以欄位名稱 (例如 result['name']) 存取
    conn = get_db_connection()
    if not conn: return [] if not fetch_one else None
    
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(query, params)
            if fetch_one:
                # 返回單筆結果 (字典格式)
                result = cur.fetchone()
                return dict(result) if result else None
            else:
                # 返回多筆結果 (字典列表格式)
                return [dict(row) for row in cur.fetchall()]
    except psycopg2.Error as e:
        print(f"DB 查詢錯誤: {e}")
        return []
    finally:
        conn.close()

# -----------------------------------------------
# 部署後，您需要在 Railway 的 CLI 中手動執行一次以下程式碼來初始化表格:
# if __name__ == '__main__':
#     initialize_db()
# -----------------------------------------------