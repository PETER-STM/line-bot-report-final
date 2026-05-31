import os

# 1. 定義正確的 requirements.txt 內容 (改用 psycopg2-binary)
requirements_content = """Flask
line-bot-sdk
psycopg2-binary
gunicorn
python-dotenv
pytz
APScheduler
"""

# 2. 寫入 requirements.txt
print("🔄 正在修復 requirements.txt ...")
with open("requirements.txt", "w", encoding="utf-8") as f:
    f.write(requirements_content)
print("✅ requirements.txt 已更新為雲端穩定版 (psycopg2-binary)")

# 3. 刪除多餘的 logic.py (如果存在)
if os.path.exists("logic.py"):
    print("🗑️ 發現舊檔案 logic.py，正在刪除...")
    os.remove("logic.py")
    print("✅ logic.py 已刪除，避免混淆")
else:
    print("👀 logic.py 不存在，無需刪除")

print("\n🚀 環境修復完成！請重新執行 'railway up' 進行部署。")