"""Constants for the EVNSPC integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "evnspc"

BASE_URL = "https://cskh.evnspc.vn"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CUSTOMER_CODE = "customer_code"
CONF_COOKIES = "cookies"
CONF_SCAN_INTERVAL = "scan_interval_minutes"

# Dữ liệu đo ghi xa chỉ cập nhật theo ngày -> không cần hỏi dồn dập.
DEFAULT_SCAN_INTERVAL = 120  # phút
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 720

# Trang web giới hạn tối đa 31 ngày mỗi lần tra cứu.
HISTORY_DAYS = 31

UPDATE_INTERVAL = timedelta(minutes=DEFAULT_SCAN_INTERVAL)
