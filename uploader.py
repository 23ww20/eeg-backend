"""
把这个文件放到 eeg_monitor.py 同目录，在主程序里 import 使用
"""
import requests
import threading
import time
from collections import deque

API_BASE = "https://你的服务器地址.railway.app"  # 部署后替换
USERNAME = "your_username"   # 替换
PASSWORD = "your_password"   # 替换

_token      = None
_upload_q   = deque(maxlen=200)   # 离线缓冲，最多200条
_lock       = threading.Lock()

def _login():
    global _token
    try:
        res = requests.post(f"{API_BASE}/api/login",
                            json={"username": USERNAME, "password": PASSWORD},
                            timeout=5)
        if res.ok and res.json().get("token"):
            _token = res.json()["token"]
            print(f"✅ 云端登录成功")
            return True
    except Exception as e:
        print(f"❌ 云端登录失败: {e}")
    return False

def _headers():
    return {"Authorization": f"Bearer {_token}", "Content-Type": "application/json"}

def upload_record(stage, theta, alpha, beta, attention, meditation, poor_signal=0):
    """线程安全：把数据放入上传队列"""
    record = {
        "stage": stage, "theta": float(theta), "alpha": float(alpha),
        "beta": float(beta), "attention": int(attention),
        "meditation": int(meditation), "poor_signal": int(poor_signal),
        "timestamp": time.time()
    }
    with _lock:
        _upload_q.append(record)

def _upload_worker():
    """后台线程：持续消费队列并上传"""
    global _token
    if not _login():
        print("❌ 云端上传功能不可用")
        return

    while True:
        time.sleep(1)
        with _lock:
            if not _upload_q:
                continue
            batch = list(_upload_q)
            _upload_q.clear()

        try:
            if len(batch) == 1:
                res = requests.post(f"{API_BASE}/api/eeg/upload",
                                    json=batch[0], headers=_headers(), timeout=5)
            else:
                res = requests.post(f"{API_BASE}/api/eeg/batch",
                                    json={"records": batch}, headers=_headers(), timeout=5)
            if res.status_code == 401:
                print("⚠️ Token过期，重新登录...")
                _login()
            elif not res.ok:
                print(f"⚠️ 上传失败({res.status_code})，数据将重试")
                with _lock:
                    _upload_q.extendleft(reversed(batch))
        except Exception as e:
            print(f"⚠️ 上传异常: {e}，数据将重试")
            with _lock:
                _upload_q.extendleft(reversed(batch))

def start_uploader():
    """在主程序启动时调用一次"""
    t = threading.Thread(target=_upload_worker, daemon=True)
    t.start()
    print("☁️ 云端上传线程已启动")
