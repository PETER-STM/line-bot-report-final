import os
from dotenv import load_dotenv

# 1. 系統啟動第一秒，強制吸收 .env 的能量
load_dotenv()

# 2. 智慧防呆：如果您的 .env 還是寫 PUBLIC_DB_URL，系統自動幫您轉成 DATABASE_URL
public_url = os.getenv('PUBLIC_DB_URL')
if public_url and not os.getenv('DATABASE_URL'):
    os.environ['DATABASE_URL'] = public_url

# 3. 最終確認是否拿到雲端門票
if not os.getenv('DATABASE_URL'):
    print("❌ 嚴重錯誤：完全找不到資料庫網址！請檢查 .env 檔案是否和 db_migrate.py 放在同一個資料夾。")
    exit()

print("✅ 成功鎖定雲端資料庫網址！準備連線...")

# 4. 確認拿到門票後，我們直接從 database.py 呼叫，徹底繞開 app.py 的陷阱！
from database import init_db

print("--- 開始執行資料庫防護網建置 ---")
init_db()
print("--- 🚀 資料庫結構更新成功完成！黑盒子已上線！ ---")