import threading

auto_mode: bool = False
auto_thread: threading.Thread | None = None
tracking_mode: bool = False
tracking_thread: threading.Thread | None = None
_lock = threading.Lock()


# 开启auto
def start_auto():
    global auto_mode
    with _lock:
        auto_mode = True

# 停止auto
def stop_auto():
    global auto_mode, auto_thread
    with _lock:
        auto_mode = False
        auto_thread = None

# 检查是否auto状态
def is_auto_running() -> bool:
    with _lock:
        return auto_mode

# 开启tracking状态
def start_tracking():
    global tracking_mode
    with _lock:
        tracking_mode = True

# 停止tracking
def stop_tracking():
    global tracking_mode, tracking_thread
    with _lock:
        tracking_mode = False
        tracking_thread = None

# 检查是否tracking
def is_tracking() -> bool:
    with _lock:
        return tracking_mode
