"""Client cho trang CSKH EVNSPC (https://cskh.evnspc.vn).

Các endpoint:
  GET  /TaiKhoan/DangNhap              -> trang đăng nhập (lấy anti-forgery token)
  GET  /TaiKhoan/GetCaptchaImage       -> ảnh captcha (gắn với session cookie)
  POST /TaiKhoan/CustomCaptcha         -> kiểm tra captcha, trả {"KetQua": "1"}
  POST /TaiKhoan/Login                 -> đăng nhập, trả {"KetQua": ..., "ThongBao": ...}
  POST /TraCuu/KiemTraDiemDo           -> trả {"MA_DDO": ..., "LOAI_DDO": "1"|"3"}
  POST /TraCuu/GetChiSoVaDienNangNgay  -> HTML bảng chỉ số + sản lượng theo ngày
"""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import aiohttp
import ddddocr
from yarl import URL

from .const import BASE_URL

_LOGGER = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
}


class EvnSpcError(Exception):
    """Lỗi chung."""


class CannotConnect(EvnSpcError):
    """Không kết nối được tới máy chủ."""


class InvalidCaptcha(EvnSpcError):
    """Captcha sai."""


class InvalidAuth(EvnSpcError):
    """Sai tài khoản / mật khẩu."""


class PasswordChangeRequired(EvnSpcError):
    """Website yêu cầu đổi mật khẩu trước khi dùng tiếp."""


class SessionExpired(EvnSpcError):
    """Phiên đăng nhập hết hạn -> cần đăng nhập lại (có captcha)."""


class NoRemoteReading(EvnSpcError):
    """Mã khách hàng không có đo ghi xa (không có sản lượng theo ngày)."""


class ParseError(EvnSpcError):
    """Không đọc được dữ liệu trả về."""


@dataclass(slots=True)
class DailyReading:
    """Một dòng dữ liệu theo ngày."""

    day: date
    index: float | None  # Chỉ số công tơ (kWh)
    kwh: float  # Sản lượng trong ngày (kWh)


@dataclass(slots=True)
class MeteringPoint:
    """Điểm đo."""

    code: str  # MA_DDO
    phases: str  # "1" hoặc "3"


# --------------------------------------------------------------------------- #
# Parsers (thuần Python, dễ test offline)
# --------------------------------------------------------------------------- #
def _num(text: str) -> float:
    """'12,420.25' -> 12420.25 ; '0,39' -> 0.39."""
    t = html_lib.unescape(text).strip().replace("\xa0", "")
    if not t:
        raise ValueError("empty")
    if "," in t and "." in t:
        t = t.replace(",", "")
    elif "," in t:
        head, _, tail = t.rpartition(",")
        t = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else t.replace(",", "")
    return float(t)


def parse_token(page: str) -> str:
    """Lấy anti-forgery token từ trang đăng nhập."""
    for pattern in (
        r'id="faceAntiForgeryToken"\s+value="([^"]+)"',
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
        r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
    ):
        m = re.search(pattern, page)
        if m:
            return html_lib.unescape(m.group(1))
    raise ParseError("Không tìm thấy RequestVerificationToken trong trang đăng nhập")


def parse_daily_html(page: str) -> list[DailyReading]:
    """Đọc bảng 'Chi tiết' (STT | Ngày | Chỉ số công tơ | Sản lượng)."""
    readings: dict[date, DailyReading] = {}

    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [
            re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if len(cells) < 4:
            continue
        try:
            day = datetime.strptime(cells[1], "%d/%m/%Y").date()
        except ValueError:
            continue
        try:
            index = _num(cells[2])
        except ValueError:
            index = None
        try:
            kwh = _num(cells[-1])
        except ValueError:
            continue
        readings[day] = DailyReading(day=day, index=index, kwh=kwh)

    if readings:
        return [readings[d] for d in sorted(readings)]

    ngay = re.search(r'id="hidenNgay"\s+value="([^"]*)"', page)
    tong = re.search(r'id="hidenTong"\s+value="([^"]*)"', page)
    if ngay and tong and ngay.group(1):
        days = ngay.group(1).split("-")
        vals = tong.group(1).split("-")
        out = []
        for d, v in zip(days, vals):
            try:
                out.append(
                    DailyReading(
                        day=datetime.strptime(d, "%d/%m/%Y").date(),
                        index=None,
                        kwh=_num(v),
                    )
                )
            except ValueError:
                continue
        return sorted(out, key=lambda r: r.day)

    return []


def looks_like_login_page(text: str) -> bool:
    return "frmDangNhapTaiKhoan" in text or "/TaiKhoan/GetCaptchaImage" in text


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class EvnSpcClient:
    """Client bất đồng bộ, dùng chung một aiohttp session (có cookie jar riêng)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token: str | None = None
        # Khởi tạo mô hình OCR nhận diện chữ/số
        self._ocr = ddddocr.DdddOcr(show_ad=False)

    # ---- cookies --------------------------------------------------------- #
    def export_cookies(self) -> dict[str, str]:
        jar = self._session.cookie_jar.filter_cookies(URL(BASE_URL))
        return {k: m.value for k, m in jar.items()}

    def import_cookies(self, cookies: dict[str, str]) -> None:
        self._session.cookie_jar.update_cookies(cookies, URL(BASE_URL))

    # ---- helpers --------------------------------------------------------- #
    def _ajax_headers(self, referer: str) -> dict[str, str]:
        h = {
            **HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}{referer}",
        }
        if self._token:
            h["RequestVerificationToken"] = self._token
        return h

    async def _request(self, method: str, path: str, **kwargs) -> aiohttp.ClientResponse:
        try:
            return await self._session.request(
                method,
                f"{BASE_URL}{path}",
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err

    # ---- captcha & đăng nhập tự động ------------------------------------- #
    async def async_solve_captcha(self, img_bytes: bytes) -> str:
        """Chạy OCR giải mã ảnh CAPTCHA trong thread pool để tránh chặn Event Loop."""
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, self._ocr.classification, img_bytes)
        return text.strip().upper()

    async def async_prepare_login(self) -> tuple[bytes, str]:
        """Mở trang đăng nhập + tải ảnh captcha. Trả (bytes, mime)."""
        resp = await self._request(
            "GET",
            "/TaiKhoan/DangNhap",
            params={"previousLink": "/TraCuu/SanLuongDienTieuThuTrongNgay"},
            headers={**HEADERS, "Accept": "text/html,*/*"},
        )
        async with resp:
            if resp.status != 200:
                raise CannotConnect(f"HTTP {resp.status} khi mở trang đăng nhập")
            self._token = parse_token(await resp.text())
        return await self.async_refresh_captcha()

    async def async_refresh_captcha(self) -> tuple[bytes, str]:
        resp = await self._request(
            "GET",
            "/TaiKhoan/GetCaptchaImage",
            params={"t": int(time.time() * 1000)},
            headers={**HEADERS, "Referer": f"{BASE_URL}/TaiKhoan/DangNhap"},
        )
        async with resp:
            if resp.status != 200:
                raise CannotConnect(f"HTTP {resp.status} khi tải captcha")
            data = await resp.read()
            mime = resp.headers.get("Content-Type", "image/png").split(";")[0]
        return data, mime

    async def async_login(self, username: str, password: str, captcha: str) -> None:
        if not self._token:
            raise EvnSpcError("Chưa gọi async_prepare_login()")

        # 1) Kiểm tra captcha
        resp = await self._request(
            "POST",
            "/TaiKhoan/CustomCaptcha",
            data={"clientCaptcha": captcha},
            headers=self._ajax_headers("/TaiKhoan/DangNhap"),
        )
        async with resp:
            try:
                cap = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise CannotConnect("Phản hồi captcha không hợp lệ") from err
        if str(cap.get("KetQua")) != "1":
            raise InvalidCaptcha(cap.get("ThongBao") or "Captcha không hợp lệ")

        # 2) Đăng nhập
        resp = await self._request(
            "POST",
            "/TaiKhoan/Login",
            data={
                "username": username,
                "password": password,
                "previousLink": "/TraCuu/SanLuongDienTieuThuTrongNgay",
            },
            headers=self._ajax_headers("/TaiKhoan/DangNhap"),
        )
        async with resp:
            try:
                res = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise CannotConnect("Phản hồi đăng nhập không hợp lệ") from err

        ket_qua = str(res.get("KetQua"))
        _LOGGER.debug("Login KetQua=%s", ket_qua)
        if ket_qua in ("0", "44"):
            raise InvalidAuth(res.get("ThongBao") or "Sai tên đăng nhập hoặc mật khẩu")
        if ket_qua == "2":
            raise PasswordChangeRequired(
                "Website yêu cầu đổi mật khẩu. Hãy đăng nhập trên web và đổi mật khẩu trước."
            )

        # Chạm vào trang tra cứu để chắc chắn cookie .EVNSPC.Auth đã hợp lệ
        await self._assert_logged_in()

    async def async_auto_login(self, username: str, password: str, max_retries: int = 3) -> None:
        """Tự động chuẩn bị phiên, tải ảnh, giải CAPTCHA và đăng nhập ngầm."""
        for attempt in range(1, max_retries + 1):
            _LOGGER.info("EVNSPC: Đang thử đăng nhập ngầm lần %d/%d...", attempt, max_retries)
            try:
                if not self._token:
                    captcha_bytes, _ = await self.async_prepare_login()
                else:
                    captcha_bytes, _ = await self.async_refresh_captcha()

                captcha_text = await self.async_solve_captcha(captcha_bytes)
                _LOGGER.debug("EVNSPC: Chuỗi CAPTCHA nhận diện: %s", captcha_text)

                await self.async_login(username, password, captcha_text)
                _LOGGER.info("EVNSPC: Đăng nhập ngầm thành công!")
                return
            except InvalidCaptcha as err:
                _LOGGER.warning("EVNSPC: CAPTCHA không chính xác (Lần %d): %s", attempt, err)
                if attempt == max_retries:
                    raise
                await asyncio.sleep(1)
            except Exception:
                raise

    async def _assert_logged_in(self) -> None:
        resp = await self._request(
            "GET",
            "/TraCuu/SanLuongDienTieuThuTrongNgay",
            headers={**HEADERS, "Accept": "text/html,*/*"},
            allow_redirects=False,
        )
        async with resp:
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
                raise InvalidAuth("Đăng nhập không thành công (bị chuyển về trang đăng nhập)")
            text = await resp.text()
            if looks_like_login_page(text) and "MaKhachHangChiSoChot" not in text:
                raise InvalidAuth("Đăng nhập không thành công")

    # ---- dữ liệu --------------------------------------------------------- #
    async def _post_json_or_html(self, path: str, data: dict, referer: str):
        resp = await self._request(
            "POST",
            path,
            data=data,
            headers=self._ajax_headers(referer),
            allow_redirects=False,
        )
        async with resp:
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
                raise SessionExpired(f"HTTP {resp.status} tại {path}")
            if resp.status >= 400:
                raise CannotConnect(f"HTTP {resp.status} tại {path}")
            text = await resp.text()
        if looks_like_login_page(text):
            raise SessionExpired("Máy chủ trả về trang đăng nhập")
        return text

    async def async_get_metering_point(self, customer_code: str) -> MeteringPoint:
        import json

        text = await self._post_json_or_html(
            "/TraCuu/KiemTraDiemDo",
            {"MaDonVi": "", "MaKhachHang": customer_code},
            "/TraCuu/SanLuongDienTieuThuTrongNgay",
        )
        try:
            data = json.loads(text)
        except ValueError as err:
            raise ParseError("KiemTraDiemDo không trả về JSON") from err

        ma_ddo = (data.get("MA_DDO") or "").strip()
        loai = (str(data.get("LOAI_DDO") or "0")).strip()
        if not ma_ddo:
            raise NoRemoteReading("Hệ thống chưa lấy được thông tin điểm đo")
        if loai not in ("1", "3"):
            raise NoRemoteReading("Khách hàng chưa có thông tin đo ghi xa")
        return MeteringPoint(code=ma_ddo, phases=loai)

    async def async_get_daily(
        self, point: MeteringPoint, start: date, end: date
    ) -> list[DailyReading]:
        text = await self._post_json_or_html(
            "/TraCuu/GetChiSoVaDienNangNgay",
            {
                "MaDiemDo": point.code,
                "TuNgay": start.strftime("%d/%m/%Y"),
                "DenNgay": end.strftime("%d/%m/%Y"),
                "SoPha": point.phases,
            },
            "/TraCuu/SanLuongDienTieuThuTrongNgay",
        )
        return parse_daily_html(text)