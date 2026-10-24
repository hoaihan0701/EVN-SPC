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
        EvnSpcStartMeterSensor(coordinator, customer_code),
        EvnSpcCurrentMeterSensor(coordinator, customer_code),
        EvnSpcEstimatedConsumptionSensor(coordinator, customer_code),
        EvnSpcConsumptionTodaySensor(coordinator, customer_code),
        EvnSpcConsumptionYesterdaySensor(coordinator, customer_code),
        EvnSpcConsumptionBeforeYesterdaySensor(coordinator, customer_code),
        EvnSpcLatestBillSensor(coordinator, customer_code),
    ] + [
        EvnSpcConsumptionMonthSensor(coordinator, customer_code, month)
        for month in range(1, 13)
    ]
    
    async_add_entities(sensors, True)

    async def handle_tra_cuu_ngay(call):
        tu_ngay = call.data.get("tu_ngay")
        den_ngay = call.data.get("den_ngay")
        result, details = await hass.async_add_executor_job(coordinator.get_consumption_by_date_range, tu_ngay, den_ngay)
        hass.components.persistent_notification.create(
            f"Tổng sản lượng từ {tu_ngay} đến {den_ngay}: **{result} kWh**\n\nChi tiết:\n{details}",
            title="EVNSPC Tra Cứu Sản Lượng Khoảng Ngày"
        )

    async def handle_tra_cuu_hoa_don(call):
        thang = call.data.get("thang")
        nam = call.data.get("nam")
        ky = call.data.get("ky", 1)
        bill_info = await hass.async_add_executor_job(coordinator.get_bill_detail_by_period, ky, thang, nam)
        hass.components.persistent_notification.create(
            f"Hóa đơn Kỳ {ky}, Tháng {thang}/{nam}:\n- Tổng tiền: **{bill_info['amount']} VNĐ**\n- Số hóa đơn: {bill_info['invoice_no']}",
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
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
        })
        self.base_url = "https://cskh.evnspc.vn"
        self.chart_data = {}
        self.latest_bill_amount = 0
        self.start_meter = 0
        self.current_meter = 0
        self.daily_consumption = {}
        self.is_logged_in = False

    def login(self):
        try:
            # Tạo mới Session để xóa toàn bộ Cookie hết hạn cũ
            self.session = requests.Session()
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            })
            
            # 1. Truy cập trực tiếp trang đăng nhập chuyên biệt /TaiKhoan/DangNhap
            login_page_url = f"{self.base_url}/TaiKhoan/DangNhap"
            res_page = self.session.get(login_page_url, timeout=10)
            soup = BeautifulSoup(res_page.text, 'html.parser')
            
            # Lấy token chống giả mạo từ form đăng nhập thực tế
            token_input = soup.select_one('input[name="__RequestVerificationToken"]') or soup.select_one('#faceAntiForgeryToken')
            token = token_input.get('value') if token_input else ""

            # 2. Gửi request POST trực tiếp tới endpoint đăng nhập
            headers = {
                "RequestVerificationToken": token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": login_page_url
            }
            payload = {
                "username": self.username,
                "password": self.password,
                "previousLink": "/TraCuu/HoaDonTienDien"
            }
            res = self.session.post(login_page_url, data=payload, headers=headers, timeout=10)
            
            if res.status_code == 200:
                if "DangNhap" not in res.url:
                    _LOGGER.info("Tạo mới phiên làm việc EVNSPC thành công!")
                    self.is_logged_in = True
                    return True
                else:
                    try:
                        data = res.json()
                        if str(data.get("KetQua")) != "0":
                            _LOGGER.info("Tạo mới phiên làm việc EVNSPC thành công!")
                            self.is_logged_in = True
                            return True
                        else:
                            _LOGGER.error(f"Đăng nhập thất bại: {data.get('ThongBao')}")
                    except Exception:
                        _LOGGER.error("Đăng nhập EVNSPC thất bại do bị chuyển hướng về trang đăng nhập.")
            
            self.is_logged_in = False
        except Exception as e:
            _LOGGER.error(f"Lỗi khi thực hiện đăng nhập lại EVNSPC: {e}")
            self.is_logged_in = False
        return False

    def ensure_authenticated_request(self, method, url, **kwargs):
        """Hàm kiểm tra phiên: Tự động đăng nhập lại nếu phát hiện hết hạn session"""
        if not self.is_logged_in:
            self.login()

        res = self.session.request(method, url, **kwargs)
        
        # Nếu bị đẩy về trang đăng nhập do hết hạn session
        if res.status_code == 200 and ("DangNhap" in res.url or "faceAntiForgeryToken" in res.text):
            _LOGGER.warning("Phiên đăng nhập EVNSPC hết hạn. Đang thực hiện tái đăng nhập...")
            if self.login():
                res = self.session.request(method, url, **kwargs)
        
        return res

    def update_data(self):
        try:
            current_year = datetime.now().year
            current_month = datetime.now().month
            
            # 1. Lấy dữ liệu biểu đồ tiêu thụ điện theo tháng
            chart_url = f"{self.base_url}/TraCuu/GetCombinedThongTinChiSo?timNam={current_year}"
            res = self.ensure_authenticated_request("GET", chart_url, timeout=10)
            if res.status_code == 200 and res.text.startswith("{"):
                data = res.json()
                self.chart_data = data.get("ChartData", {})

            # 2. Lấy thông tin hóa đơn tiền điện
            bill_url = f"{self.base_url}/TraCuu/ThongTinHoaDonTienDien"
            params = {"ky": "1", "thang": current_month, "nam": current_year}
            res_bill = self.ensure_authenticated_request("GET", bill_url, params=params, timeout=10)
            if res_bill.status_code == 200:
                soup = BeautifulSoup(res_bill.text, 'html.parser')
                row = soup.select_one('.result-tbl tbody tr')
                if row:
                    cols = row.find_all('td')
                    if len(cols) >= 5:
                        amount_str = cols[4].text.strip().replace('.', '').replace(',', '.')
                        self.latest_bill_amount = float(amount_str) if amount_str else 0

            # 3. Lấy chỉ số công tơ và sản lượng ngày qua API đo ghi xa
            check_dd_url = f"{self.base_url}/TraCuu/KiemTraDiemDo"
            res_dd = self.ensure_authenticated_request("POST", check_dd_url, data={"MaDonVi": "", "MaKhachHang": self.customer_code}, timeout=10)
            if res_dd.status_code == 200 and res_dd.text.startswith("{"):
                dd_data = res_dd.json()
                ma_diem_do = dd_data.get("MA_DDO", "").strip()
                loai_ddo = dd_data.get("LOAI_DDO", "0").strip()
                
                if ma_diem_do and loai_ddo in ["1", "3"]:
                    today = datetime.now()
                    den_ngay = today - timedelta(days=1)
                    tu_ngay = today - timedelta(days=35)
                    
                    daily_url = f"{self.base_url}/TraCuu/GetChiSoVaDienNangNgay"
                    res_daily = self.ensure_authenticated_request("POST", daily_url, data={
                        "MaDiemDo": ma_diem_do,
                        "TuNgay": tu_ngay.strftime("%d/%m/%Y"),
                        "DenNgay": den_ngay.strftime("%d/%m/%Y"),
                        "SoPha": loai_ddo
                    }, timeout=10)
                    
                    if res_daily.status_code == 200:
                        soup_d = BeautifulSoup(res_daily.text, 'html.parser')
                        tables = soup_d.select('.result-tbl')
                        if len(tables) >= 2:
                            tr_list = tables[1].select('tbody tr')
                            meter_values = []
                            for idx, tr in enumerate(tr_list):
                                tds = tr.find_all('td')
                                if len(tds) >= 4:
                                    ngay_val = tds[1].text.strip()
                                    chi_so_val = float(tds[2].text.strip().replace('.', '').replace(',', '.'))
                                    san_luong_val = float(tds[3].text.strip().replace('.', '').replace(',', '.'))
                                    
                                    self.daily_consumption[ngay_val] = {
                                        "kwh": san_luong_val,
                                        "meter": chi_so_val
                                    }
                                    meter_values.append(chi_so_val)
                            
                            if meter_values:
                                self.start_meter = meter_values[0]
                                self.current_meter = meter_values[-1]

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

    def get_data_by_date(self, target_date: datetime):
        date_str = target_date.strftime("%d/%m/%Y")
        return self.daily_consumption.get(date_str, {"kwh": 0, "meter": 0})

    def get_consumption_by_date_range(self, tu_ngay_str, den_ngay_str):
        try:
            t_date = datetime.strptime(tu_ngay_str.replace('-', '/'), "%d/%m/%Y")
            e_date = datetime.strptime(den_ngay_str.replace('-', '/'), "%d/%m/%Y")
            total = 0
            curr = t_date
            details = ""
            while curr <= e_date:
                data = self.get_data_by_date(curr)
                total += data["kwh"]
                details += f"- {curr.strftime('%d/%m/%Y')}: {data['kwh']} kWh (Chỉ số: {data['meter']})\n"
                curr += timedelta(days=1)
            return round(total, 2), details
        except Exception as e:
            _LOGGER.error(f"Lỗi tính khoảng ngày: {e}")
            return 0, "Lỗi định dạng ngày"

    def get_bill_detail_by_period(self, ky, thang, nam):
        try:
            bill_url = f"{self.base_url}/TraCuu/HoaDonTienDien"
            params = {"ky": str(ky), "thang": str(thang), "nam": str(nam)}
            res = self.ensure_authenticated_request("GET", bill_url, params=params, timeout=10)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                row = soup.select_one('.result-tbl tbody tr')
                if row:
                    cols = row.find_all('td')
                    if len(cols) >= 5:
                        invoice_no = cols[3].text.strip()
                        amount_str = cols[4].text.strip().replace('.', '').replace(',', '.')
                        return {
                            "amount": float(amount_str) if amount_str else 0,
                            "invoice_no": invoice_no
                        }
        except Exception as e:
            _LOGGER.error(f"Lỗi lấy hóa đơn kỳ {ky} tháng {thang}/{nam}: {e}")
        return {"amount": 0, "invoice_no": "Không tìm thấy"}


class EvnSpcStartMeterSensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Chỉ Số Đầu Kỳ ({customer_code})"
        self._attr_unique_id = f"evnspc_start_meter_{customer_code}"
        self._attr_icon = "mdi:counter"

    def update(self) -> None:
        self._coordinator.update_data()
        self._attr_native_value = self._coordinator.start_meter

class EvnSpcCurrentMeterSensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Chỉ Số Hiện Tại ({customer_code})"
        self._attr_unique_id = f"evnspc_current_meter_{customer_code}"
        self._attr_icon = "mdi:counter"

    def update(self) -> None:
        self._coordinator.update_data()
        self._attr_native_value = self._coordinator.current_meter

class EvnSpcEstimatedConsumptionSensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Tiêu Thụ Tạm Tính Tháng ({customer_code})"
        self._attr_unique_id = f"evnspc_estimated_consumption_{customer_code}"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:flash-outline"

    def update(self) -> None:
        self._coordinator.update_data()
        self._attr_native_value = round(max(0, self._coordinator.current_meter - self._coordinator.start_meter), 2)

class EvnSpcConsumptionTodaySensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Tiêu Thụ Hôm Nay ({customer_code})"
        self._attr_unique_id = f"evnspc_consumption_today_{customer_code}"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:lightning-bolt"

    def update(self) -> None:
        yesterday = datetime.now() - timedelta(days=1)
        self._attr_native_value = self._coordinator.get_data_by_date(yesterday)["kwh"]

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
        self._attr_native_value = self._coordinator.get_data_by_date(yesterday)["kwh"]

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
        self._attr_native_value = self._coordinator.get_data_by_date(before_yesterday)["kwh"]

class EvnSpcLatestBillSensor(SensorEntity):
    def __init__(self, coordinator, customer_code):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._attr_name = f"EVNSPC Tiền Điện Tháng Gần Nhất ({customer_code})"
        self._attr_unique_id = f"evnspc_latest_bill_{customer_code}"
        self._attr_native_unit_of_measurement = "VND"
        self._attr_icon = "mdi:cash-marker"

    def update(self) -> None:
        self._attr_native_value = self._coordinator.latest_bill_amount

class EvnSpcConsumptionMonthSensor(SensorEntity):
    def __init__(self, coordinator, customer_code, month):
        self._coordinator = coordinator
        self._customer_code = customer_code
        self._month = month
        self._attr_name = f"EVNSPC Tiêu Thụ Tháng {month} ({customer_code})"
        self._attr_unique_id = f"evnspc_consumption_month_{month}_{customer_code}"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:chart-bar"

    def update(self) -> None:
        self._attr_native_value = self._coordinator.get_consumption_by_month(self._month)
