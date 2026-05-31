# -*- coding: utf-8 -*-
import os
import psycopg2
from psycopg2 import pool

DATABASE_URL = os.getenv('DATABASE_URL')
db_pool = None

def get_db_connection():
    global db_pool
    if not db_pool:
        try:
            db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, DATABASE_URL, sslmode='prefer')
            print("✅ DB Pool Ready")
        except Exception as e:
            print(f"❌ Pool Error: {e}")
            return None
    return db_pool.getconn()

def close_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def init_db():
    """初始化資料庫 (V20.1：包含預設資料的邏輯修正)"""
    conn = get_db_connection()
    if not conn: 
        print("❌ Init DB failed: No connection")
        return

    try:
        with conn.cursor() as cur:
            # 1. 建立地點表
            cur.execute("""CREATE TABLE IF NOT EXISTS locations (
                location_name VARCHAR(50) PRIMARY KEY,
                weekday_cost INTEGER DEFAULT 0,
                weekend_cost INTEGER DEFAULT 0,
                surcharge INTEGER DEFAULT 0,
                linked_monthly_item VARCHAR(50),
                category VARCHAR(50) DEFAULT '一般',
                monthly_rent INTEGER DEFAULT 0,
                cleaning_fee INTEGER DEFAULT 0,
                business_days VARCHAR(50),
                shared_members TEXT
            );""")
            cur.execute("""CREATE TABLE IF NOT EXISTS error_logs (
                log_id SERIAL PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_type VARCHAR(50),
                error_message TEXT,
                original_input TEXT
            );""")

            # 建立審計軌跡保險箱 (Audit Logs，用來追蹤刪除與合併)
            cur.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
                audit_id SERIAL PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                action_type VARCHAR(50),
                target_table VARCHAR(50),
                record_details TEXT,
                performed_by VARCHAR(50) DEFAULT 'system'
            );""")
            
            # 補齊可能缺失的欄位
            required_cols = [
                ("weekday_cost", "INT DEFAULT 0"), 
                ("weekend_cost", "INT DEFAULT 0"), 
                ("surcharge", "INT DEFAULT 0"), 
                ("category", "VARCHAR(50) DEFAULT '一般'"),
                ("monthly_rent", "INT DEFAULT 0"), 
                ("cleaning_fee", "INT DEFAULT 0"),
                ("business_days", "VARCHAR(50)"), 
                ("shared_members", "TEXT")
            ]
            for col_name, col_type in required_cols:
                try: 
                    cur.execute(f"ALTER TABLE locations ADD COLUMN {col_name} {col_type}")
                    conn.commit()
                except: 
                    conn.rollback()

            # 2. 地點別名表
            cur.execute("""CREATE TABLE IF NOT EXISTS location_aliases (
                alias_name VARCHAR(50) PRIMARY KEY,
                target_location VARCHAR(50) REFERENCES locations(location_name) ON DELETE CASCADE
            );""")

            # 3. 人員別名表
            cur.execute("""CREATE TABLE IF NOT EXISTS member_aliases (
                alias_name VARCHAR(50) PRIMARY KEY,
                target_name VARCHAR(50)
            );""")

            # 4. 人員與專案表
            cur.execute("CREATE TABLE IF NOT EXISTS members (name VARCHAR(50) PRIMARY KEY);")
            cur.execute("""CREATE TABLE IF NOT EXISTS projects (
                project_id SERIAL PRIMARY KEY, record_date DATE NOT NULL,
                location_name VARCHAR(50) REFERENCES locations(location_name),
                total_fixed_cost INTEGER NOT NULL, original_msg TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
            cur.execute("""CREATE TABLE IF NOT EXISTS project_members (
                project_id INTEGER REFERENCES projects(project_id) ON DELETE CASCADE,
                member_name VARCHAR(50) REFERENCES members(name), PRIMARY KEY (project_id, member_name)
            );""")
            
            # 5. 紀錄表
            cur.execute("""CREATE TABLE IF NOT EXISTS records (
                record_id SERIAL PRIMARY KEY, record_date DATE NOT NULL,
                member_name VARCHAR(50) REFERENCES members(name),
                project_id INTEGER REFERENCES projects(project_id) ON DELETE CASCADE,
                cost_paid INTEGER DEFAULT 0, original_msg TEXT
            );""")
            
            # 自動修復 record_id
            try:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='records' AND column_name='record_id'")
                if not cur.fetchone():
                    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='records' AND column_name='id'")
                    if cur.fetchone():
                        cur.execute("ALTER TABLE records RENAME COLUMN id TO record_id")
                    else:
                        cur.execute("ALTER TABLE records ADD COLUMN record_id SERIAL PRIMARY KEY")
                    conn.commit()
            except: pass

            # --- [V20.1] 預設資料更新 (邏輯修正) ---
            # 這裡的邏輯是：如果只有一個價格，就同時套用到平日與假日
            updates = [
                ('總站', 570, '一般'), # 修正：總站改為一般固定價 570 (260+310)
                ('三和', 372, '月租'), ('通化', 209, '月租'),
                ('大慶', 800, '台中'), ('旱溪', 800, '台中'), ('捷運', 800, '台中'),
                ('市集', 400, '台中'), ('太平市集', 400, '台中'), ('西屯市集', 400, '台中'),
                ('饒河', 400, '台北'), ('饒河2', 600, '台北'), ('重新', 200, '台北'), 
                ('樂華', 500, '台北'), ('宏匯', 0, '台北'),
                ('草鞋墩', 500, '一般') # 草鞋墩預設先給500，之後用指令改成 500/750
            ]
            
            for loc, cost, cat in updates:
                # 注意：這裡把 cost 同時填入 weekday_cost 和 weekend_cost
                cur.execute("""INSERT INTO locations (location_name, weekday_cost, weekend_cost, category) 
                    VALUES (%s, %s, %s, %s) ON CONFLICT (location_name) 
                    DO UPDATE SET category=EXCLUDED.category 
                    -- 只更新類別，避免覆蓋掉使用者已經手動修改過的價格
                """, (loc, cost, cost, cat))
            
            # 補上饒河別名
            cur.execute("INSERT INTO location_aliases (alias_name, target_location) VALUES ('饒河305', '饒河') ON CONFLICT DO NOTHING")
            
            # [特別設定] 草鞋墩的假日價 (如果有需要預設)
            cur.execute("UPDATE locations SET weekend_cost = 750 WHERE location_name = '草鞋墩'")

            conn.commit()
    except Exception as e:
        print(f"❌ DB Init Error: {e}")
        conn.rollback()
    finally:
        close_db_connection(conn)