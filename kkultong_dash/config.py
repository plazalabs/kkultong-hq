"""환경변수 및 .env 파일 설정 로더 (표준 라이브러리 전용)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from . import (
    DASHBOARD_PAGE_TITLE,
    DEFAULT_COMPETITION_DATA_SOURCE_ID,
    DEFAULT_EXPERIENCE_DATA_SOURCE_ID,
    DEFAULT_SETTLEMENT_PAGE_ID,
    FACT_LEDGER_DB_TITLE,
    NOTION_API_BASE_URL,
    NOTION_API_VERSION,
    PARENT_PAGE_TITLE,
)

_NOTION_HEX32_RE = re.compile(r"([0-9a-fA-F]{32})(?:[/?#]|$)")
_NOTION_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _format_uuid32(hex32: str) -> str:
    h = hex32.lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def normalize_notion_id(raw: Optional[str]) -> Optional[str]:
    """collection:// 접두사, Notion 페이지 URL, 공백을 정리해 Notion UUID 문자열을 반환한다."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.startswith("collection://"):
        cleaned = cleaned[len("collection://") :].strip()
    if cleaned.startswith(("http://", "https://")):
        m_uuid = _NOTION_UUID_RE.search(cleaned)
        if m_uuid:
            return m_uuid.group(1).lower()
        m_hex = _NOTION_HEX32_RE.search(cleaned)
        if m_hex:
            return _format_uuid32(m_hex.group(1))
    if len(cleaned) == 32 and all(c in "0123456789abcdefABCDEF" for c in cleaned):
        return _format_uuid32(cleaned)
    return cleaned or None


def parse_dotenv_file(path: Path) -> dict[str, str]:
    """간단한 .env 파일을 파싱한다 (외부 패키지 불필요)."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key] = value
    return result


def load_dotenv(env_file: Optional[Path] = None, repo_root: Optional[Path] = None) -> Optional[Path]:
    """프로세스 환경변수에 없는 키만 .env에서 채운다 (기존 환경변수 우선)."""
    candidates: list[Path] = []
    if env_file is not None:
        candidates.append(env_file)
    else:
        candidates.append(Path.cwd() / ".env")
        if repo_root is not None and repo_root != Path.cwd():
            candidates.append(repo_root / ".env")

    for candidate in candidates:
        if candidate.is_file():
            parsed = parse_dotenv_file(candidate)
            for key, value in parsed.items():
                if key not in os.environ or not os.environ[key]:
                    os.environ[key] = value
            return candidate
    return None


@dataclass(frozen=True)
class AppConfig:
    """실행 설정."""

    notion_token: str
    experience_data_source_id: str = DEFAULT_EXPERIENCE_DATA_SOURCE_ID
    competition_data_source_id: str = DEFAULT_COMPETITION_DATA_SOURCE_ID
    parent_page_id: Optional[str] = None
    dashboard_page_id: Optional[str] = None
    settlement_page_id: Optional[str] = DEFAULT_SETTLEMENT_PAGE_ID
    fact_ledger_database_id: Optional[str] = None
    fact_ledger_data_source_id: Optional[str] = None
    parent_page_title: str = PARENT_PAGE_TITLE
    dashboard_page_title: str = DASHBOARD_PAGE_TITLE
    fact_ledger_db_title: str = FACT_LEDGER_DB_TITLE
    db_path: Path = Path("data/kkultong_dash.db")
    api_base_url: str = NOTION_API_BASE_URL
    api_version: str = NOTION_API_VERSION
    min_request_interval_sec: float = 0.35
    request_timeout_sec: float = 30.0
    max_retry_attempts: int = 6
    max_retry_after_sec: float = 120.0
    fact_revalidate_host_delay_sec: float = 3.0

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        *,
        repo_root: Optional[Path] = None,
        db_path_override: Optional[Path] = None,
        require_token: bool = True,
    ) -> "AppConfig":
        env = environ if environ is not None else os.environ
        token = (env.get("NOTION_TOKEN") or "").strip()
        if require_token and not token:
            raise ValueError(
                "환경변수 NOTION_TOKEN이 설정되지 않았습니다. "
                "실행 환경의 환경변수 또는 .env 파일에 NOTION_TOKEN을 지정하세요."
            )

        exp_id = (
            normalize_notion_id(env.get("NOTION_EXPERIENCE_DATA_SOURCE_ID"))
            or DEFAULT_EXPERIENCE_DATA_SOURCE_ID
        )
        comp_id = (
            normalize_notion_id(env.get("NOTION_COMPETITION_DATA_SOURCE_ID"))
            or DEFAULT_COMPETITION_DATA_SOURCE_ID
        )
        parent_id = normalize_notion_id(env.get("NOTION_PARENT_PAGE_ID"))
        dashboard_id = normalize_notion_id(env.get("NOTION_DASHBOARD_PAGE_ID"))

        raw_settlement = env.get("NOTION_SETTLEMENT_PAGE_ID")
        if raw_settlement is not None and raw_settlement.strip().lower() in {"none", "off", "false"}:
            settlement_id = None
        else:
            settlement_id = normalize_notion_id(raw_settlement) or DEFAULT_SETTLEMENT_PAGE_ID

        fact_db_id = normalize_notion_id(env.get("NOTION_FACT_LEDGER_DATABASE_ID"))
        fact_ds_id = normalize_notion_id(env.get("NOTION_FACT_LEDGER_DATA_SOURCE_ID"))

        default_db = (repo_root / "data" / "kkultong_dash.db") if repo_root else Path("data/kkultong_dash.db")
        raw_db_path = (env.get("KKULTONG_DB_PATH") or "").strip()
        db_path = db_path_override or (Path(raw_db_path) if raw_db_path else default_db)

        min_interval = _parse_float(env.get("NOTION_MIN_INTERVAL_SEC"), default=0.35, minimum=0.0)
        timeout = _parse_float(env.get("NOTION_TIMEOUT_SEC"), default=30.0, minimum=1.0)
        max_attempts = int(_parse_float(env.get("NOTION_MAX_RETRY_ATTEMPTS"), default=6.0, minimum=1.0))
        max_retry_after = _parse_float(env.get("NOTION_MAX_RETRY_AFTER_SEC"), default=120.0, minimum=1.0)
        host_delay = _parse_float(env.get("FACT_REVALIDATE_HOST_DELAY_SEC"), default=3.0, minimum=0.0)

        return cls(
            notion_token=token,
            experience_data_source_id=exp_id,
            competition_data_source_id=comp_id,
            parent_page_id=parent_id,
            dashboard_page_id=dashboard_id,
            settlement_page_id=settlement_id,
            fact_ledger_database_id=fact_db_id,
            fact_ledger_data_source_id=fact_ds_id,
            db_path=db_path,
            min_request_interval_sec=min_interval,
            request_timeout_sec=timeout,
            max_retry_attempts=max_attempts,
            max_retry_after_sec=max_retry_after,
            fact_revalidate_host_delay_sec=host_delay,
        )

    def safe_summary(self) -> dict[str, str]:
        """비밀값 없이 설정 상태를 요약한다."""
        return {
            "NOTION_TOKEN": "설정됨 (마스킹됨)" if self.notion_token else "미설정",
            "NOTION_API_VERSION": self.api_version,
            "NOTION_EXPERIENCE_DATA_SOURCE_ID": self.experience_data_source_id,
            "NOTION_COMPETITION_DATA_SOURCE_ID": self.competition_data_source_id,
            "NOTION_PARENT_PAGE_ID": self.parent_page_id or "(미지정 — 제목 검색 사용)",
            "NOTION_DASHBOARD_PAGE_ID": self.dashboard_page_id or "(미지정 — SQLite/하위페이지 탐색 후 필요 시 생성)",
            "NOTION_SETTLEMENT_PAGE_ID": self.settlement_page_id or "(비활성화됨)",
            "NOTION_FACT_LEDGER_DATABASE_ID": self.fact_ledger_database_id or "(미지정 — 하위 DB 탐색/생성)",
            "NOTION_FACT_LEDGER_DATA_SOURCE_ID": self.fact_ledger_data_source_id or "(미지정 — DB 메타에서 자동 확인)",
            "KKULTONG_DB_PATH": str(self.db_path),
            "NOTION_MIN_INTERVAL_SEC": str(self.min_request_interval_sec),
            "FACT_REVALIDATE_HOST_DELAY_SEC": str(self.fact_revalidate_host_delay_sec),
        }


def _parse_float(raw: Optional[str], *, default: float, minimum: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw.strip())
    except ValueError:
        return default
    return max(parsed, minimum)
