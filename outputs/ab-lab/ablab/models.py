import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Rules(StrictModel):
    blacklist: list[str] = Field(default_factory=list, max_length=500)
    whitelist: list[str] = Field(default_factory=list, max_length=500)
    block_bots: bool = False
    strict_bots: bool = False
    bot_markers: list[str] = Field(default_factory=lambda: ['bot', 'crawler', 'spider'], max_length=100)
    block_ipv4: bool = False
    block_pc: bool = False
    min_android: int = Field(default=0, ge=0, le=100)
    min_ios: int = Field(default=0, ge=0, le=100)
    blocked_cidrs: list[str] = Field(default_factory=list, max_length=500)
    countries: list[str] = Field(default_factory=list, max_length=250)
    languages: list[str] = Field(default_factory=list, max_length=100)
    max_visits: int = Field(default=0, ge=0, le=1000000)
    window_hours: int = Field(default=24, ge=1, le=720)

    @field_validator('blacklist', 'whitelist', 'blocked_cidrs')
    @classmethod
    def networks(cls, values):
        return list(dict.fromkeys(str(ipaddress.ip_network(v.strip(), strict=False)) for v in values))

    @field_validator('countries')
    @classmethod
    def country_codes(cls, values):
        result = [v.strip().upper() for v in values]
        if any(not re.fullmatch('[A-Z]{2}', v) for v in result):
            raise ValueError('国家/地区须为两位代码，例如 CN、HK、US')
        return list(dict.fromkeys(result))

    @field_validator('languages')
    @classmethod
    def language_codes(cls, values):
        result = [v.strip().lower() for v in values]
        if any(not re.fullmatch('[a-z]{2,3}(?:-[a-z0-9]{2,8})*', v) for v in result):
            raise ValueError('语言格式无效，例如 zh、en、zh-cn')
        return list(dict.fromkeys(result))

    @field_validator('bot_markers')
    @classmethod
    def markers(cls, values):
        result = [v.strip().lower() for v in values]
        if any(not v or len(v) > 100 or any(ord(c) < 32 for c in v) for v in result):
            raise ValueError('UA 特征须为 1–100 个可见字符')
        return list(dict.fromkeys(result))


class Config(StrictModel):
    routing: Literal['RULES', 'FORCE_A', 'FORCE_B'] = 'RULES'
    allowed_slot: Literal['A', 'B'] = 'B'
    protection: bool = True
    content_mode: Literal['PAGE', 'LINK'] = 'PAGE'
    distribution: Literal['random', 'round_robin', 'equal'] = 'round_robin'
    rules: Rules = Field(default_factory=Rules)


class ConfigUpdate(StrictModel):
    config: Config
    revision: int = Field(ge=0)


class VersionChoice(StrictModel):
    version_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class SourceEdit(StrictModel):
    path: str = Field(min_length=1, max_length=240, strict=True)
    content: str = Field(strict=True)
    expected_published: str | None = Field(pattern=r'^[a-f0-9]{32}$')


class LinkInput(StrictModel):
    slot: Literal['A', 'B']
    urls: list[str] = Field(min_length=1, max_length=100)

    @field_validator('urls')
    @classmethod
    def safe_urls(cls, values):
        result = []
        for value in values:
            value = value.strip()
            if len(value) > 2048 or any(ord(c) <= 32 for c in value) or '\\' in value:
                raise ValueError('链接含非法字符或过长')
            url = urlsplit(value)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                raise ValueError('仅支持不含账号密码的 HTTP(S) 完整链接')
            # Force port validation; fragments are allowed but never logged.
            _ = url.port
            result.append(value)
        return list(dict.fromkeys(result))


class RedirectPresetInput(StrictModel):
    urls: list[str] = Field(min_length=1, max_length=30)
    note: str = Field(default='', max_length=300, strict=True)

    @field_validator('urls')
    @classmethod
    def public_urls(cls, values):
        from .linkcheck import normalize_http_url
        return list(dict.fromkeys(normalize_http_url(value.strip()) for value in values))


class RedirectSplitMember(StrictModel):
    preset_id: int = Field(gt=0, strict=True)
    weight: int = Field(default=1, ge=1, le=100, strict=True)


class RedirectSplit(StrictModel):
    enabled: bool
    mode: Literal['random', 'weighted']
    members: list[RedirectSplitMember] = Field(default_factory=list, max_length=30)

    @field_validator('members')
    @classmethod
    def unique_members(cls, values):
        if len({item.preset_id for item in values}) != len(values):
            raise ValueError('分流链接重复')
        return values


class RedirectApply(StrictModel):
    version_id: str = Field(pattern=r'^[a-f0-9]{32}$', strict=True)
    preset_id: int = Field(gt=0, strict=True)
    occurrence_ids: list[str] = Field(min_length=1, max_length=5000)
    expected_published: str | None = Field(pattern=r'^[a-f0-9]{32}$')

    @field_validator('occurrence_ids')
    @classmethod
    def span_ids(cls, values):
        if any(not re.fullmatch(r'[a-f0-9]{64}', value) for value in values) or len(set(values)) != len(values):
            raise ValueError('跳转位置标识无效或重复，请重新扫描')
        return values


def normalize_whatsapp_phone(value):
    digits = re.sub(r'\D', '', str(value).strip())
    if not re.fullmatch(r'\d{8,15}', digits):
        raise ValueError('WhatsApp 号码需为 8 到 15 位数字')
    return digits


class WhatsAppNumberInput(StrictModel):
    phones: list[str] = Field(min_length=1, max_length=30)
    note: str = Field(default='', max_length=300, strict=True)

    @field_validator('phones')
    @classmethod
    def public_phones(cls, values):
        return list(dict.fromkeys(normalize_whatsapp_phone(value) for value in values))


class WhatsAppNumberApply(StrictModel):
    version_id: str = Field(pattern=r'^[a-f0-9]{32}$', strict=True)
    number_id: int = Field(gt=0, strict=True)
    occurrence_ids: list[str] = Field(min_length=1, max_length=5000)
    expected_published: str | None = Field(pattern=r'^[a-f0-9]{32}$')

    @field_validator('occurrence_ids')
    @classmethod
    def span_ids(cls, values):
        if any(not re.fullmatch(r'[a-f0-9]{64}', value) for value in values) or len(set(values)) != len(values):
            raise ValueError('跳转位置标识无效或重复，请重新扫描')
        return values


class Visitor(StrictModel):
    ip: str = Field(max_length=64)
    country: str | None = Field(default=None, pattern=r'^[A-Z]{2}$')
    ua: str = Field(default='', max_length=1024)
    language: str = Field(default='', max_length=512)
    visits: int = Field(default=1, ge=0, le=1000000000)

    @field_validator('ip')
    @classmethod
    def normalize_ip(cls, value):
        return str(ipaddress.ip_address(value))
