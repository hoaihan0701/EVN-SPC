import voluptuous as vol
import requests
from bs4 import BeautifulSoup
from homeassistant import config_entries
from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD, CONF_CUSTOMER_CODE

class EvnspcConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            
            # Kiểm tra đăng nhập thử nghiệm trước khi lưu
            login_success = await self.hass.async_add_executor_job(
                self._test_login, username, password
            )
            
            if login_success:
                return self.async_create_entry(
                    title=f"EVNSPC ({user_input[CONF_CUSTOMER_CODE]})",
                    data=user_input
                )
            else:
                errors["base"] = "invalid_auth"

        schema = vol.Schema({
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Required(CONF_CUSTOMER_CODE): str,
        })

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors
        )

    def _test_login(self, username, password):
        try:
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            })
            base_url = "https://cskh.evnspc.vn"
            
            # 1. Truy cập trang đăng nhập để lấy token chống giả mạo
            login_page_url = f"{base_url}/TaiKhoan/DangNhap"
            res_page = session.get(login_page_url, timeout=10)
            soup = BeautifulSoup(res_page.text, 'html.parser')
            
            token_input = soup.select_one('input[name="__RequestVerificationToken"]') or soup.select_one('#faceAntiForgeryToken')
            token = token_input.get('value') if token_input else ""

            # 2. Gửi request đăng nhập thử
            headers = {
                "RequestVerificationToken": token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": login_page_url
            }
            payload = {
                "username": username,
                "password": password,
                "previousLink": "/TraCuu/HoaDonTienDien"
            }
            res = session.post(login_page_url, data=payload, headers=headers, timeout=10)
            
            if res.status_code == 200:
                if "DangNhap" not in res.url:
                    return True
                try:
                    data = res.json()
                    if str(data.get("KetQua")) != "0":
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False
