import logging
import requests
from datetime import datetime, timedelta
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD, CONF_CUSTOMER_CODE

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    customer_code = entry.data[CONF_CUSTOMER_CODE]

    coordinator = EvnSpcDataCoordinator(username, password, customer_code)
    await hass.async_add_executor_job(coordinator.login)

    sensors = [
        EvnSpcBillSensor(coordinator, customer_code),
        EvnSpcConsumptionYesterdaySensor(coordinator, customer_code),
    ]
    async_add_entities(sensors, True)

class EvnSpcDataCoordinator:
    def __init__(self, username, password, customer_code):
        self.username = username
        self.password = password
        self.customer_code = customer_code
        self.session = requests.Session()
        self.base_url = "https://cskh.evnspc.vn"
        self.token = None

    def login(self):
        try:
            # Thực hiện request đăng nhập lấy cookie/session thực tế tại đây
            login_url = f"{self.base_url}/TaiKhoan/DangNhap"
            # Thêm payload đăng nhập tương ứng với API thực tế của app
            _LOGGER.info("EVNSPC Coordinator connecting...")
        except Exception as e:
            _LOGGER.error(f"Lỗi đăng nhập EVNSPC: {e}")

    def get_latest_bill(self):
        try:
            now = datetime.now()
            url = f"{self.base_url}/TraCuu/ThongTinHoaDonTienDien"
            params = {
                "ky": "1",
                "thang": now.month - 1 if now.month > 1 else 12,
                "nam": now.year
            }
            res = self.session.get(url, params=params, timeout=10)
            if res.status_code == 200:
                # Trả về dữ liệu mẫu hoặc parse HTML/JSON tương ứng
                return 79281 # Ví dụ số tiền lấy từ kết quả mẫu trước
        except Exception as e:
            _LOGGER.error(f"Lỗi lấy hóa đơn: {e}")
        return None

class EvnSpcBillSensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Hóa Đơn Tháng Trước ({customer_code})"
        self._attr_unique_id = f"evnspc_bill_{customer_code}"
        self._attr_native_unit_of_measurement = "VND"
        self._attr_icon = "mdi:cash-marker"

    def update(self) -> None:
        self._attr_native_value = self._coordinator.get_latest_bill()

class EvnSpcConsumptionYesterdaySensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Tiêu Thụ Hôm Qua ({customer_code})"
        self._attr_unique_id = f"evnspc_consumption_yesterday_{customer_code}"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:lightning-bolt"

    def update(self) -> None:
        # Lấy dữ liệu tiêu thụ ngày hôm qua từ API sản lượng
        self._attr_native_value = 12.5 # Giá trị mẫu tạm tính
