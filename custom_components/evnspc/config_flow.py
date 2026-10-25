"""Config flow: tài khoản + mã khách hàng + captcha."""
from __future__ import annotations

import base64
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from . import build_client
from .api import (
    CannotConnect,
    EvnSpcClient,
    EvnSpcError,
    InvalidAuth,
    InvalidCaptcha,
    NoRemoteReading,
    ParseError,
    PasswordChangeRequired,
)
from .const import (
    CONF_COOKIES,
    CONF_CUSTOMER_CODE,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

CUSTOMER_CODE_RE = re.compile(r"^(PB|PK|PC|GK)[A-Z0-9]{11}$")
CONF_CAPTCHA = "captcha"


class EvnSpcConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._client: EvnSpcClient | None = None
        self._data: dict[str, Any] = {}
        self._captcha_md = ""
        self._reauth_entry: ConfigEntry | None = None

    # ---- helpers --------------------------------------------------------- #
    def _set_captcha(self, raw: bytes, mime: str) -> None:
        b64 = base64.b64encode(raw).decode()
        self._captcha_md = f"![captcha](data:{mime};base64,{b64})"

    async def _start_login(self) -> None:
        """Tạo session mới + tải captcha."""
        self._client = build_client(self.hass)
        raw, mime = await self._client.async_prepare_login()
        self._set_captcha(raw, mime)

    async def _refresh_captcha(self) -> None:
        assert self._client is not None
        raw, mime = await self._client.async_refresh_captcha()
        self._set_captcha(raw, mime)

    def _captcha_schema(self, with_password: bool = False) -> vol.Schema:
        fields: dict = {}
        if with_password:
            fields[
                vol.Required(CONF_PASSWORD, default=self._data.get(CONF_PASSWORD, ""))
            ] = str
        fields[vol.Required(CONF_CAPTCHA)] = str
        return vol.Schema(fields)

    # ---- bước 1: thông tin tài khoản ------------------------------------ #
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_CUSTOMER_CODE].strip().upper()
            if not CUSTOMER_CODE_RE.match(code):
                errors[CONF_CUSTOMER_CODE] = "invalid_customer_code"
            else:
                await self.async_set_unique_id(code)
                self._abort_if_unique_id_configured()
                self._data = {
                    CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_CUSTOMER_CODE: code,
                }
                try:
                    await self._start_login()
                except (CannotConnect, ParseError):
                    errors["base"] = "cannot_connect"
                else:
                    return await self.async_step_captcha()

        defaults = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(
                        CONF_CUSTOMER_CODE,
                        default=defaults.get(CONF_CUSTOMER_CODE, ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    # ---- bước 2: captcha -------------------------------------------------- #
    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._client is not None

        if user_input is not None:
            try:
                await self._client.async_login(
                    self._data[CONF_USERNAME],
                    self._data[CONF_PASSWORD],
                    user_input[CONF_CAPTCHA].strip(),
                )
                await self._client.async_get_metering_point(
                    self._data[CONF_CUSTOMER_CODE]
                )
            except InvalidCaptcha:
                errors["base"] = "invalid_captcha"
                await self._try_refresh()
            except InvalidAuth:
                return await self._back_to_user("invalid_auth")
            except PasswordChangeRequired:
                return self.async_abort(reason="password_change_required")
            except NoRemoteReading:
                return self.async_abort(reason="no_remote_reading")
            except (CannotConnect, ParseError):
                errors["base"] = "cannot_connect"
                await self._try_refresh()
            except EvnSpcError:
                _LOGGER.exception("Lỗi không xác định khi đăng nhập EVNSPC")
                errors["base"] = "unknown"
                await self._try_refresh()
            else:
                return self.async_create_entry(
                    title=f"EVNSPC {self._data[CONF_CUSTOMER_CODE]}",
                    data={**self._data, CONF_COOKIES: self._client.export_cookies()},
                )

        return self.async_show_form(
            step_id="captcha",
            data_schema=self._captcha_schema(),
            description_placeholders={"captcha_image": self._captcha_md},
            errors=errors,
        )

    async def _try_refresh(self) -> None:
        try:
            await self._refresh_captcha()
        except EvnSpcError:
            pass

    async def _back_to_user(self, error: str) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=self._data.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(
                        CONF_CUSTOMER_CODE,
                        default=self._data.get(CONF_CUSTOMER_CODE, ""),
                    ): str,
                }
            ),
            errors={"base": error},
        )

    # ---- đăng nhập lại khi phiên hết hạn --------------------------------- #
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self._get_reauth_entry()
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is None or self._client is None:
            try:
                await self._start_login()
            except (CannotConnect, ParseError):
                errors["base"] = "cannot_connect"
        else:
            password = user_input[CONF_PASSWORD]
            try:
                await self._client.async_login(
                    self._data[CONF_USERNAME],
                    password,
                    user_input[CONF_CAPTCHA].strip(),
                )
            except InvalidCaptcha:
                errors["base"] = "invalid_captcha"
                await self._try_refresh()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
                try:
                    await self._start_login()
                except EvnSpcError:
                    pass
            except PasswordChangeRequired:
                return self.async_abort(reason="password_change_required")
            except (CannotConnect, ParseError):
                errors["base"] = "cannot_connect"
                await self._try_refresh()
            else:
                assert self._reauth_entry is not None
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data_updates={
                        CONF_PASSWORD: password,
                        CONF_COOKIES: self._client.export_cookies(),
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._captcha_schema(with_password=True),
            description_placeholders={
                "captcha_image": self._captcha_md,
                "customer_code": self._data.get(CONF_CUSTOMER_CODE, ""),
            },
            errors=errors,
        )

    # ---- options ---------------------------------------------------------- #
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EvnSpcOptionsFlow()


class EvnSpcOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )
