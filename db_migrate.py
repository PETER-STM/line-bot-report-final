import os
import sys

# 嘗試從 app.py 導入 init_db 函數
try:
    from app import init_db
except ImportError as e:
    # 這通常發生在 app.py 載入時失敗 (例如缺少模組或環境變數崩潰)
    print(f"錯誤：無法從 app.py 導入 init_db。請檢查 app.py 依賴項和環境變數設置。", file=sys.stderr)
    print(f"詳細錯誤: {e}", file=sys.stderr)
    sys.exit(1)

def run_migration():
    """
    專門用於在本地連線遠端資料庫並執行 init_db 的函數。
    """
    # 檢查是否有 PUBLIC_DB_URL 變數 (這是我們在 CMD 中設定的公開連線 URL)
    public_db_url = os.getenv('PUBLIC_DB_URL')
    
    if public_db_url:
        print(f"偵測到 PUBLIC_DB_URL，將使用此公開 URL 覆蓋 DATABASE_URL。")
        # 覆蓋環境變數，以繞過 Railway 內部地址解析失敗問題
        os.environ['DATABASE_URL'] = public_db_url
    else:
        print("警告：未偵測到 PUBLIC_DB_URL，將使用預設的 DATABASE_URL (可能無法在本地解析內部域名)。")
        # 程式將使用 Railway 預設的內部 DATABASE_URL，預計會失敗，但仍讓它執行。

    print("--- 開始執行 V7.1 資料庫結構更新 (init_db) ---")
    
    try:
        # 執行資料庫初始化/更新
        init_db(force_recreate=False)
        print("--- 資料庫結構更新成功完成！---")
    except Exception as e:
        print(f"嚴重錯誤：執行 init_db 失敗！請檢查 DATABASE_URL 是否能正確連線。錯誤資訊: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    run_migration()