"""DataUpdateCoordinator cho EVNSPC."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CannotConnect,
    DailyReading,
    EvnSpcClient,
    EvnSpcError,
    InvalidAuth,
    InvalidCaptcha,
    MeteringPoint,
    NoRemoteReading,
    ParseError,
    PasswordChangeRequired,
    SessionExpired,
)
from .const import (
    CONF_COOKIES,
    CONF_CUSTOMER_CODE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HISTORY_DAYS,
)

try:  # HA >= 2025.4
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover
    StatisticMeanType = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(slots=True)
class EvnSpcData:
    """Dữ liệu đã xử lý."""

    readings: list[DailyReading] = field(default_factory=list)

    @property
    def latest(self) -> DailyReading | None:
        return self.readings[-1] if self.readings else None

    def total_last_days(self, days: int) -> float:
        if not self.readings:
            return 0.0
        last = self.readings[-1].day
        since = last - timedelta(days=days - 1)
        return round(sum(r.kwh for r in self.readings if r.day >= since), 3)

    def month_total(self) -> float:
        if not self.readings:
            return 0.0
        last = self.readings[-1].day
        return round(
            sum(
                r.kwh
                for r in self.readings
                if (r.day.year, r.day.month) == (last.year, last.month)
            ),
            3,
        )


class EvnSpcCoordinator(DataUpdateCoordinator[EvnSpcData]):
    """Lấy dữ liệu định kỳ."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EvnSpcClient
    ) -> None:
        minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.data[CONF_CUSTOMER_CODE]}",
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self.customer_code: str = entry.data[CONF_CUSTOMER_CODE]
        self._point: MeteringPoint | None = None
        self._store: Store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")

    # ------------------------------------------------------------------ #
    async def _async_fetch_readings(self) -> list[DailyReading]:
        """Thực hiện các bước kéo dữ liệu đo ghi xa."""
        if self._point is None:
            self._point = await self.client.async_get_metering_point(self.customer_code)

        end = datetime.now(VN_TZ).date() - timedelta(days=1)
        start = end - timedelta(days=HISTORY_DAYS - 1)
        return await self.client.async_get_daily(self._point, start, end)

    async def _async_update_data(self) -> EvnSpcData:
        try:
            readings = await self._async_fetch_readings()
        except SessionExpired:
            _LOGGER.info("EVNSPC: Phiên làm việc đã hết hạn. Đang tiến hành tự giải CAPTCHA và đăng nhập ngầm...")
            username = self.config_entry.data.get(CONF_USERNAME) or self.customer_code
            password = self.config_entry.data.get(CONF_PASSWORD)

            if not username or not password:
                raise ConfigEntryAuthFailed(
                    "Không tìm thấy thông tin đăng nhập trong cấu hình để tự động làm mới phiên"
                )

            try:
                # Tự động lấy ảnh, OCR giải CAPTCHA và đăng nhập lại
                await self.client.async_auto_login(username, password, max_retries=3)
                # Đăng nhập xong, reset lại point để lấy lại session sạch
                self._point = None
                readings = await self._async_fetch_readings()
            except (InvalidCaptcha, InvalidAuth, PasswordChangeRequired) as auth_err:
                _LOGGER.error("EVNSPC: Đăng nhập ngầm thất bại: %s", auth_err)
                raise ConfigEntryAuthFailed(f"Xác thực EVNSPC thất bại: {auth_err}") from auth_err
            except (CannotConnect, ParseError, EvnSpcError) as err:
                raise UpdateFailed(f"Lỗi kết nối khi tự đăng nhập lại EVNSPC: {err}") from err

        except NoRemoteReading as err:
            raise UpdateFailed(str(err)) from err
        except (CannotConnect, ParseError, EvnSpcError) as err:
            raise UpdateFailed(f"Lỗi khi lấy dữ liệu EVNSPC: {err}") from err

        if not readings:
            if self.data and self.data.readings:
                _LOGGER.debug("Chưa có dữ liệu mới, giữ dữ liệu cũ")
                return self.data
            raise UpdateFailed("EVNSPC chưa trả về dữ liệu sản lượng nào")

        await self._async_save_cookies()
        await self._async_import_statistics(readings)
        return EvnSpcData(readings=readings)

    # ------------------------------------------------------------------ #
    async def _async_save_cookies(self) -> None:
        cookies = self.client.export_cookies()
        if cookies and cookies != self.config_entry.data.get(CONF_COOKIES):
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_COOKIES: cookies},
            )

    async def _async_import_statistics(self, readings: list[DailyReading]) -> None:
        """Ghi thống kê dài hạn đúng theo ngày để dùng trong bảng Năng lượng."""
        points = [r for r in readings if r.index is not None]
        if not points:
            return

        stored = await self._store.async_load() or {}
        base = stored.get("base_index")
        if base is None:
            first = points[0]
            base = round(first.index - first.kwh, 3)
            await self._store.async_save({"base_index": base})

        stats = [
            StatisticData(
                start=datetime.combine(r.day, time(0, 0), tzinfo=VN_TZ),
                state=r.index,
                sum=round(r.index - base, 3),
            )
            for r in points
        ]

        meta_kwargs: dict = {
            "has_sum": True,
            "name": f"EVNSPC {self.customer_code} - điện tiêu thụ",
            "source": DOMAIN,
            "statistic_id": f"{DOMAIN}:consumption_{self.customer_code.lower()}",
            "unit_of_measurement": "kWh",
        }
        if StatisticMeanType is not None:
            meta_kwargs["mean_type"] = StatisticMeanType.NONE
        else:
            meta_kwargs["has_mean"] = False

        async_add_external_statistics(self.hass, StatisticMetaData(**meta_kwargs), stats)