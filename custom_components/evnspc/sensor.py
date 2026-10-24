import logging
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
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
    await hass.async_add_executor_job(coordinator.update_data)

    sensors = [
        EvnSpcBillSensor(coordinator, customer_code),
        EvnSpcConsumptionYesterdaySensor(coordinator, customer_code),
        EvnSpcConsumptionBeforeYesterdaySensor(coordinator, customer_code),
    ] + [
        EvnSpcConsumptionMonthSensor(coordinator, customer_code, month)
        for month in range(1, 13)
    ]
    
    async_add_entities(sensors, True)

    # Đăng ký các service mở rộng phục vụ tra cứu linh hoạt theo yêu cầu
    async def handle_tra_cuu_ngay(call):
        tu_ngay = call.data.get("tu_ngay")
        den_ngay = call.data.get("den_ngay")
        result = await hass.async_add_executor_job(coordinator.get_consumption_by_date_range, tu_ngay, den_ngay)
        hass.components.persistent_notification.create(
            f"Sản lượng từ {tu_ngay} đến {den_ngay}: {result} kWh",
            title="EVNSPC Tra Cứu Khoảng Ngày"
        )

    async def handle_tra_cuu_hoa_don(call):
        thang = call.data.get("thang")
        nam = call.data.get("nam")
        ky = call.data.get("ky", 1)
        bill = await hass.async_add_executor_job(coordinator.get_bill_by_period, ky, thang, nam)
        hass.components.persistent_notification.create(
            f"Hóa đơn Kỳ {ky}, Tháng {thang}/{nam}: {bill} VNĐ",
            title="EVNSPC Tra Cứu Hóa Đơn"
        )

    hass.services.async_register(DOMAIN, "tra_cuu_san_luong_theo_ngay", handle_tra_cuu_ngay)
    hass.services.async_register(DOMAIN, "tra_cuu_hoa_don_theo_thang", handle_tra_cuu_hoa_don)


class EvnSpcDataCoordinator:
    def __init__(self, username, password, customer_code):
        self.username = username
        self.password = password
        self.customer_code = customer_code
        self.session = requests.Session()
        self.base_url = "https://cskh.evnspc.vn"
        self.chart_data = {}
        self.latest_bill_amount = 0
        self.daily_consumption = {} # Lưu sản lượng theo ngày {"24/10/2026": 0.49, ...}

    def update_data(self):
        try:
            current_year = datetime.now().year
            current_month = datetime.now().month
            
            # 1. Lấy dữ liệu biểu đồ tiêu thụ điện theo tháng
            chart_url = f"{self.base_url}/TraCuu/GetCombinedThongTinChiSo?timNam={current_year}"
            res = self.session.get(chart_url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                self.chart_data = data.get("ChartData", {})

            # 2. Lấy thông tin hóa đơn tiền điện
            bill_url = f"{self.base_url}/TraCuu/ThongTinHoaDonTienDien"
            params = {"ky": "1", "thang": current_month, "nam": current_year}
            res_bill = self.session.get(bill_url, params=params, timeout=10)
            if res_bill.status_code == 200:
                soup = BeautifulSoup(res_bill.text, 'html.parser')
                row = soup.select_one('.result-tbl tbody tr')
                if row:
                    cols = row.find_all('td')
                    if len(cols) >= 5:
                        amount_str = cols[4].text.strip().replace('.', '').replace(',', '.')
                        self.latest_bill_amount = float(amount_str) if amount_str else 0

            # 3. Lấy sản lượng tiêu thụ theo ngày (Mặc định lấy 30 ngày gần đây dựa theo API mới bắt)
            # Trước tiên cần lấy mã điểm đo (MaDiemDo) bằng cách kiểm tra điểm đo
            check_dd_url = f"{self.base_url}/TraCuu/KiemTraDiemDo"
            res_dd = self.session.post(check_dd_url, data={"MaDonVi": "", "MaKhachHang": self.customer_code}, timeout=10)
            if res_dd.status_code == 200:
                dd_data = res_dd.json()
                ma_diem_do = dd_data.get("MA_DDO", "").strip()
                loai_ddo = dd_data.get("LOAI_DDO", "0").strip()
                
                if ma_diem_do and loai_ddo in ["1", "3"]:
                    today = datetime.now()
                    den_ngay = today - timedelta(days=1) # Dữ liệu đo ghi xa có đến ngày hôm qua
                    tu_ngay = today - timedelta(days=35)
                    
                    ngay_str = tu_ngay.strftime("%d/%m/%Y")
                    den_str = den_ngay.strftime("%d/%m/%Y")
                    
                    daily_url = f"{self.base_url}/TraCuu/GetChiSoVaDienNangNgay"
                    res_daily = self.session.post(daily_url, data={
                        "MaDiemDo": ma_diem_do,
                        "TuNgay": ngay_str,
                        "DenNgay": den_str,
                        "SoPha": loai_ddo
                    }, timeout=10)
                    
                    if res_daily.status_code == 200:
                        # Parse HTML trả về từ endpoint GetChiSoVaDienNangNgay
                        soup_d = BeautifulSoup(res_daily.text, 'html.parser')
                        rows = soup_d.select('.search-quantity-box .result-tbl tbody tr')
                        # Lưu ý bảng chi tiết nằm ở bảng thứ hai trong phần kết quả
                        tables = soup_d.select('.result-tbl')
                        if len(tables) >= 2:
                            tr_list = tables[1].select('tbody tr')
                            for tr in tr_list:
                                tds = tr.find_all('td')
                                if len(tds) >= 4:
                                    ngay_val = tds[1].text.strip()
                                    san_luong_val = float(tds[3].text.strip().replace(',', '.'))
                                    self.daily_consumption[ngay_val] = san_luong_val

            _LOGGER.info("Cập nhật dữ liệu EVNSPC thành công!")
        except Exception as e:
            _LOGGER.error(f"Lỗi cập nhật dữ liệu EVNSPC: {e}")

    def get_consumption_by_month(self, month):
        try:
            lst_data = self.chart_data.get("lstData", [])
            if lst_data and len(lst_data) > 0:
                month_items = lst_data[0]
                for item in month_items:
                    if item.get("Thang") == month:
                        return item.get("DienTieuThu", 0)
        except Exception as e:
            _LOGGER.error(f"Lỗi parse tháng {month}: {e}")
        return 0

    def get_consumption_by_date(self, target_date: datetime):
        date_str = target_date.strftime("%d/%m/%Y")
        return self.daily_consumption.get(date_str, 0)

    def get_consumption_by_date_range(self, tu_ngay_str, den_ngay_str):
        # tu_ngay_str, den_ngay_str định dạng dd/MM/yyyy hoặc dd-MM-yyyy
        try:
            t_date = datetime.strptime(tu_ngay_str.replace('-', '/'), "%d/%m/%Y")
            e_date = datetime.strptime(den_ngay_str.replace('-', '/'), "%d/%m/%Y")
            total = 0
            curr = t_date
            while curr <= e_date:
                total += self.get_consumption_by_date(curr)
                curr += timedelta(days=1)
            return round(total, 2)
        except Exception as e:
            _LOGGER.error(f"Lỗi tính khoảng ngày: {e}")
            return 0

    def get_bill_by_period(self, ky, thang, nam):
        try:
            bill_url = f"{self.base_url}/TraCuu/ThongTinHoaDonTienDien"
            params = {"ky": str(ky), "thang": str(thang), "nam": str(nam)}
            res = self.session.get(bill_url, params=params, timeout=10)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                row = soup.select_one('.result-tbl tbody tr')
                if row:
                    cols = row.find_all('td')
                    if len(cols) >= 5:
                        amount_str = cols[4].text.strip().replace('.', '').replace(',', '.')
                        return float(amount_str) if amount_str else 0
        except Exception as e:
            _LOGGER.error(f"Lỗi lấy hóa đơn kỳ {ky} tháng {thang}/{nam}: {e}")
        return 0


class EvnSpcBillSensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Tiền Điện Tháng ({customer_code})"
        self._attr_unique_id = f"evnspc_bill_{customer_code}"
        self._attr_native_unit_of_measurement = "VND"
        self._attr_icon = "mdi:cash-marker"

    def update(self) -> None:
        self._coordinator.update_data()
        self._attr_native_value = self._coordinator.latest_bill_amount


class EvnSpcConsumptionYesterdaySensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Tiêu Thụ Hôm Qua ({customer_code})"
        self._attr_unique_id = f"evnspc_consumption_yesterday_{customer_code}"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:lightning-bolt"

    def update(self) -> None:
        yesterday = datetime.now() - timedelta(days=1)
        self._attr_native_value = self._coordinator.get_consumption_by_date(yesterday)


class EvnSpcConsumptionBeforeYesterdaySensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Tiêu Thụ Hôm Kia ({customer_code})"
        self._attr_unique_id = f"evnspc_consumption_before_yesterday_{customer_code}"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:lightning-bolt"

    def update(self) -> None:
        before_yesterday = datetime.now() - timedelta(days=2)
        self._attr_native_value = self._coordinator.get_consumption_by_date(before_yesterday)


class EvnSpcConsumptionMonthSensor(SensorEntity):
    def __init__(self, coordinator, customer_code, month):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._month = month
        self._attr_name = f"EVNSPC Tiêu Thụ Tháng {month} ({customer_code})"
        self._attr_unique_id = f"evnspc_consumption_month_{month}_{customer_code}"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:lightning-bolt"

    def update(self) -> None:
        self._attr_native_value = self._coordinator.get_consumption_by_month(self._month)
