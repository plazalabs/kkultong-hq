"""SQLite 로컬 캐시 및 증분/일일 전체 동기화 관리.

동작 원칙:
- 읽은 행을 SQLite에 저장하고, 다음 실행부터는 last_edited_time 기준으로 바뀐 행만 다시 읽는다.
- 하루 1회(KST 날짜 기준 첫 실행)는 전체를 다시 읽어 기존 캐시 행 수와 다르면 경고를 생성한다.
- 조회 실패(API 오류·빈 응답·권한 없음) 시 이전 캐시 숫자를 정상인 것처럼 반환하지 않고 ok=False를 반환한다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import KST
from .models import FactAlert, SourceSyncResult
from .notion_client import NotionAPIError, NotionClient
from .parsers import parse_iso_datetime


@dataclass(frozen=True)
class SyncMeta:
    source_key: str
    last_sync_at: Optional[str]
    last_full_sync_date: Optional[str]
    last_max_edited_time: Optional[str]
    last_row_count: Optional[int]
    last_status: Optional[str]
    last_error: Optional[str]


class LocalCache:
    """SQLite 기반 로컬 캐시 및 상태 저장소."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path) if str(db_path) != ":memory:" else db_path
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LocalCache":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_rows (
                    source_key TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    created_time TEXT,
                    last_edited_time TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (source_key, page_id)
                );

                CREATE INDEX IF NOT EXISTS idx_source_rows_edited
                    ON source_rows (source_key, last_edited_time);

                CREATE TABLE IF NOT EXISTS sync_meta (
                    source_key TEXT PRIMARY KEY,
                    last_sync_at TEXT,
                    last_full_sync_date TEXT,
                    last_max_edited_time TEXT,
                    last_row_count INTEGER,
                    last_status TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ran_at_kst TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manual_section_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at_kst TEXT NOT NULL,
                    dashboard_page_id TEXT NOT NULL,
                    blocks_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fact_revalidation_skip_list (
                    url TEXT PRIMARY KEY,
                    page_id TEXT,
                    reason TEXT NOT NULL,
                    added_at_kst TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fact_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id TEXT NOT NULL,
                    claim TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    url TEXT,
                    old_grade TEXT,
                    new_grade TEXT,
                    detected_at_kst TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fact_source_doc_state (
                    page_id TEXT PRIMARY KEY,
                    source_url TEXT,
                    article_updated_at TEXT NOT NULL,
                    last_checked_at_kst TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------
    # 앱 상태 / 이력 저장
    # ------------------------------------------------------------------

    def get_state(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str, *, now_utc: Optional[datetime] = None) -> None:
        ts = (now_utc or datetime.now(timezone.utc)).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, ts),
            )

    def record_run(self, ran_at_kst: datetime, status: str, details: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO sync_runs (ran_at_kst, status, details_json)
                VALUES (?, ?, ?)
                """,
                (
                    ran_at_kst.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
                    status,
                    json.dumps(details, ensure_ascii=False),
                ),
            )

    def save_manual_section_snapshot(
        self,
        captured_at_kst: datetime,
        dashboard_page_id: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        """섹션 E 보존 구간 블록의 로컬 백업 스냅샷을 저장한다."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO manual_section_snapshots (captured_at_kst, dashboard_page_id, blocks_json)
                VALUES (?, ?, ?)
                """,
                (
                    captured_at_kst.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
                    dashboard_page_id,
                    json.dumps(blocks, ensure_ascii=False),
                ),
            )

    # ------------------------------------------------------------------
    # P2 팩트 장부 재검증 제외 목록(로그인 필요) 및 팩트 경보 저장
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_skip_url(url: str) -> str:
        return url.strip()

    def is_url_in_skip_list(self, url: Optional[str]) -> bool:
        """로그인 필요 등으로 재시도 금지 목록에 등록된 URL인지 확인한다."""
        if not url or not url.strip():
            return False
        norm = self._normalize_skip_url(url)
        row = self._conn.execute(
            "SELECT 1 FROM fact_revalidation_skip_list WHERE url = ?",
            (norm,),
        ).fetchone()
        return row is not None

    def add_url_to_skip_list(
        self,
        url: str,
        *,
        page_id: Optional[str] = None,
        reason: str = "로그인 필요",
        added_at_kst: Optional[datetime] = None,
    ) -> None:
        """로그인 필요 URL을 재시도 금지 목록에 영구 기록한다 (우회/재시도 금지)."""
        norm = self._normalize_skip_url(url)
        if not norm:
            return
        ts = (added_at_kst or datetime.now(KST)).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO fact_revalidation_skip_list (url, page_id, reason, added_at_kst)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    page_id = COALESCE(excluded.page_id, fact_revalidation_skip_list.page_id),
                    reason = excluded.reason,
                    added_at_kst = excluded.added_at_kst
                """,
                (norm, page_id, reason, ts),
            )

    def list_skip_urls(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT url, page_id, reason, added_at_kst FROM fact_revalidation_skip_list ORDER BY url ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def replace_fact_alerts(
        self,
        alerts: list[FactAlert],
        *,
        checked_at_kst: Optional[datetime] = None,
    ) -> None:
        """최신 팩트 재검증 경보 목록을 저장한다 (대시보드 섹션 A '팩트 경보' 표시용)."""
        ts_default = (checked_at_kst or datetime.now(KST)).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
        with self._conn:
            self._conn.execute("DELETE FROM fact_alerts")
            self._conn.executemany(
                """
                INSERT INTO fact_alerts (
                    page_id, claim, topic_key, reason, url, old_grade, new_grade, detected_at_kst
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        a.page_id,
                        a.claim,
                        a.topic_key,
                        a.reason,
                        a.url,
                        a.old_grade,
                        a.new_grade,
                        a.detected_at_kst or ts_default,
                    )
                    for a in alerts
                ],
            )

    def load_fact_alerts(self) -> list[FactAlert]:
        """저장된 '팩트 경보' 항목 목록을 불러온다."""
        rows = self._conn.execute(
            """
            SELECT page_id, claim, topic_key, reason, url, old_grade, new_grade, detected_at_kst
            FROM fact_alerts
            ORDER BY id ASC
            """
        ).fetchall()
        return [
            FactAlert(
                page_id=str(r["page_id"]),
                claim=str(r["claim"]),
                topic_key=str(r["topic_key"]),
                reason=str(r["reason"]),
                url=r["url"],
                old_grade=r["old_grade"],
                new_grade=r["new_grade"],
                detected_at_kst=r["detected_at_kst"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # P2.5 원문 갱신 감시 (article.updated_at) 및 등급 이력 스냅샷
    # ------------------------------------------------------------------

    def get_fact_source_updated_at(self, page_id: str) -> Optional[str]:
        """특정 팩트 장부 행에 대해 직전 실행에서 기록된 원문 article.updated_at을 조회한다."""
        pid = (page_id or "").strip()
        if not pid:
            return None
        row = self._conn.execute(
            "SELECT article_updated_at FROM fact_source_doc_state WHERE page_id = ?",
            (pid,),
        ).fetchone()
        return str(row["article_updated_at"]) if row else None

    def save_fact_source_updated_at(
        self,
        page_id: str,
        *,
        source_url: Optional[str],
        article_updated_at: str,
        checked_at_kst: Optional[datetime] = None,
    ) -> None:
        """행별 원문 JSON의 article.updated_at 값을 로컬 DB에 저장(갱신)한다."""
        pid = (page_id or "").strip()
        updated = (article_updated_at or "").strip()
        if not pid or not updated:
            return
        ts = (checked_at_kst or datetime.now(KST)).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO fact_source_doc_state (page_id, source_url, article_updated_at, last_checked_at_kst)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(page_id) DO UPDATE SET
                    source_url = COALESCE(excluded.source_url, fact_source_doc_state.source_url),
                    article_updated_at = excluded.article_updated_at,
                    last_checked_at_kst = excluded.last_checked_at_kst
                """,
                (pid, source_url, updated, ts),
            )

    # ------------------------------------------------------------------
    # 데이터 소스 캐시 조회/갱신
    # ------------------------------------------------------------------

    def get_sync_meta(self, source_key: str) -> Optional[SyncMeta]:
        row = self._conn.execute(
            "SELECT * FROM sync_meta WHERE source_key = ?", (source_key,)
        ).fetchone()
        if not row:
            return None
        return SyncMeta(
            source_key=row["source_key"],
            last_sync_at=row["last_sync_at"],
            last_full_sync_date=row["last_full_sync_date"],
            last_max_edited_time=row["last_max_edited_time"],
            last_row_count=row["last_row_count"],
            last_status=row["last_status"],
            last_error=row["last_error"],
        )

    def count_cached_rows(self, source_key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM source_rows WHERE source_key = ?", (source_key,)
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def load_cached_rows(self, source_key: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT raw_json FROM source_rows WHERE source_key = ? ORDER BY page_id ASC",
            (source_key,),
        ).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]

    def needs_full_sync(
        self,
        source_key: str,
        today_kst_str: str,
        *,
        force_full: bool = False,
    ) -> bool:
        if force_full:
            return True
        meta = self.get_sync_meta(source_key)
        if meta is None:
            return True
        if not meta.last_full_sync_date or meta.last_full_sync_date != today_kst_str:
            return True
        if not meta.last_max_edited_time:
            return True
        if meta.last_status != "ok":
            return True
        if self.count_cached_rows(source_key) == 0:
            return True
        return False

    def _save_sync_error(self, source_key: str, error_message: str, now_utc_iso: str) -> None:
        meta = self.get_sync_meta(source_key)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO sync_meta (
                    source_key, last_sync_at, last_full_sync_date,
                    last_max_edited_time, last_row_count, last_status, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    last_sync_at = excluded.last_sync_at,
                    last_status = excluded.last_status,
                    last_error = excluded.last_error
                """,
                (
                    source_key,
                    now_utc_iso,
                    meta.last_full_sync_date if meta else None,
                    meta.last_max_edited_time if meta else None,
                    meta.last_row_count if meta else None,
                    "error",
                    error_message,
                ),
            )

    def sync_data_source(
        self,
        client: NotionClient,
        *,
        source_key: str,
        source_label: str,
        data_source_id: str,
        now_kst: datetime,
        force_full: bool = False,
    ) -> SourceSyncResult:
        """데이터 소스를 증분 또는 일일 전체 동기화한다.

        실패 시 캐시의 이전 데이터를 반환하지 않고 ok=False 와 명확한 원인을 반환한다.
        """
        today_kst_str = now_kst.astimezone(KST).strftime("%Y-%m-%d")
        now_utc_iso = now_kst.astimezone(timezone.utc).isoformat()
        meta = self.get_sync_meta(source_key)
        do_full = self.needs_full_sync(source_key, today_kst_str, force_full=force_full)
        sync_mode = "full" if do_full else "incremental"

        try:
            if do_full:
                return self._sync_full(
                    client,
                    source_key=source_key,
                    source_label=source_label,
                    data_source_id=data_source_id,
                    today_kst_str=today_kst_str,
                    now_utc_iso=now_utc_iso,
                    prior_meta=meta,
                )
            return self._sync_incremental(
                client,
                source_key=source_key,
                source_label=source_label,
                data_source_id=data_source_id,
                now_utc_iso=now_utc_iso,
                prior_meta=meta,
            )
        except NotionAPIError as exc:
            err = f"[{source_label}] {exc}"
            self._save_sync_error(source_key, err, now_utc_iso)
            return SourceSyncResult(
                source_key=source_key,
                source_label=source_label,
                ok=False,
                sync_mode=sync_mode,
                error_message=err,
            )
        except Exception as exc:  # pragma: no cover - defensive catch
            err = f"[{source_label}] 예상치 못한 동기화 오류: {type(exc).__name__}: {exc}"
            self._save_sync_error(source_key, err, now_utc_iso)
            return SourceSyncResult(
                source_key=source_key,
                source_label=source_label,
                ok=False,
                sync_mode=sync_mode,
                error_message=err,
            )

    def _sync_full(
        self,
        client: NotionClient,
        *,
        source_key: str,
        source_label: str,
        data_source_id: str,
        today_kst_str: str,
        now_utc_iso: str,
        prior_meta: Optional[SyncMeta],
    ) -> SourceSyncResult:
        had_prior_cache = prior_meta is not None and prior_meta.last_full_sync_date is not None
        pre_full_count = self.count_cached_rows(source_key)

        pages, is_incomplete = client.query_data_source(data_source_id)
        active_pages = [p for p in pages if not _is_inactive(p)]

        if len(active_pages) == 0:
            err = (
                f"[{source_label}] 빈 응답 (전체 조회 결과 활성 행 0건 — "
                "통합 공유 권한, 데이터 소스 ID 또는 DB 상태를 확인하세요)"
            )
            self._save_sync_error(source_key, err, now_utc_iso)
            return SourceSyncResult(
                source_key=source_key,
                source_label=source_label,
                ok=False,
                sync_mode="full",
                row_count=0,
                fetched_count=0,
                error_message=err,
            )

        warnings: list[str] = []
        if is_incomplete:
            warnings.append(
                f"[{source_label}] 쿼리 결과 10,000건 상한(request_status=incomplete) 도달 — 일부 행이 누락되었을 수 있음"
            )

        live_count = len(active_pages)
        if had_prior_cache and live_count != pre_full_count:
            diff = live_count - pre_full_count
            warnings.append(
                f"일일 전체 재동기화 행 수 불일치 ({source_label}): "
                f"로컬 캐시 {pre_full_count}건 → 전체 조회 {live_count}건 ({diff:+d}건). "
                "삭제/보관 또는 외부 변경 가능성 — 캐시를 전체 조회 기준으로 재정렬함"
            )

        max_edited = _compute_max_edited_time(active_pages) or now_utc_iso

        with self._conn:
            self._conn.execute("DELETE FROM source_rows WHERE source_key = ?", (source_key,))
            self._conn.executemany(
                """
                INSERT INTO source_rows (
                    source_key, page_id, created_time, last_edited_time, raw_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        source_key,
                        str(p["id"]),
                        p.get("created_time"),
                        str(p.get("last_edited_time") or now_utc_iso),
                        json.dumps(p, ensure_ascii=False),
                        now_utc_iso,
                    )
                    for p in active_pages
                    if p.get("id")
                ],
            )
            self._conn.execute(
                """
                INSERT INTO sync_meta (
                    source_key, last_sync_at, last_full_sync_date,
                    last_max_edited_time, last_row_count, last_status, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(source_key) DO UPDATE SET
                    last_sync_at = excluded.last_sync_at,
                    last_full_sync_date = excluded.last_full_sync_date,
                    last_max_edited_time = excluded.last_max_edited_time,
                    last_row_count = excluded.last_row_count,
                    last_status = excluded.last_status,
                    last_error = NULL
                """,
                (
                    source_key,
                    now_utc_iso,
                    today_kst_str,
                    max_edited,
                    live_count,
                    "ok",
                ),
            )

        return SourceSyncResult(
            source_key=source_key,
            source_label=source_label,
            ok=True,
            sync_mode="full",
            row_count=live_count,
            fetched_count=live_count,
            warnings=warnings,
            rows_raw=active_pages,
        )

    def _sync_incremental(
        self,
        client: NotionClient,
        *,
        source_key: str,
        source_label: str,
        data_source_id: str,
        now_utc_iso: str,
        prior_meta: Optional[SyncMeta],
    ) -> SourceSyncResult:
        assert prior_meta is not None and prior_meta.last_max_edited_time is not None
        since_iso = _subtract_overlap_window(prior_meta.last_max_edited_time, minutes=1)

        changed_pages, is_incomplete = client.query_data_source(
            data_source_id,
            last_edited_on_or_after=since_iso,
        )

        warnings: list[str] = []
        if is_incomplete:
            warnings.append(
                f"[{source_label}] 증분 쿼리 결과 10,000건 상한(request_status=incomplete) 도달"
            )

        with self._conn:
            for page in changed_pages:
                page_id = page.get("id")
                if not page_id:
                    continue
                if _is_inactive(page):
                    self._conn.execute(
                        "DELETE FROM source_rows WHERE source_key = ? AND page_id = ?",
                        (source_key, str(page_id)),
                    )
                else:
                    self._conn.execute(
                        """
                        INSERT INTO source_rows (
                            source_key, page_id, created_time, last_edited_time, raw_json, synced_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_key, page_id) DO UPDATE SET
                            created_time = excluded.created_time,
                            last_edited_time = excluded.last_edited_time,
                            raw_json = excluded.raw_json,
                            synced_at = excluded.synced_at
                        """,
                        (
                            source_key,
                            str(page_id),
                            page.get("created_time"),
                            str(page.get("last_edited_time") or now_utc_iso),
                            json.dumps(page, ensure_ascii=False),
                            now_utc_iso,
                        ),
                    )

            all_rows = self.load_cached_rows(source_key)
            max_edited = _compute_max_edited_time(changed_pages, baseline=prior_meta.last_max_edited_time)
            self._conn.execute(
                """
                UPDATE sync_meta
                SET last_sync_at = ?,
                    last_max_edited_time = ?,
                    last_row_count = ?,
                    last_status = 'ok',
                    last_error = NULL
                WHERE source_key = ?
                """,
                (now_utc_iso, max_edited, len(all_rows), source_key),
            )

        return SourceSyncResult(
            source_key=source_key,
            source_label=source_label,
            ok=True,
            sync_mode="incremental",
            row_count=len(all_rows),
            fetched_count=len(changed_pages),
            warnings=warnings,
            rows_raw=all_rows,
        )


def _is_inactive(page: dict[str, Any]) -> bool:
    return bool(page.get("in_trash") or page.get("is_archived") or page.get("archived"))


def _compute_max_edited_time(
    pages: list[dict[str, Any]],
    *,
    baseline: Optional[str] = None,
) -> Optional[str]:
    best_dt = parse_iso_datetime(baseline) if baseline else None
    best_raw = baseline
    for p in pages:
        raw = p.get("last_edited_time")
        if not isinstance(raw, str):
            continue
        dt = parse_iso_datetime(raw)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_raw = raw
    return best_raw


def _subtract_overlap_window(iso_timestamp: str, *, minutes: int = 1) -> str:
    """last_edited_time 경계 누락을 막기 위해 1분 겹침 구간을 두고 UTC ISO 문자열로 반환한다."""
    dt = parse_iso_datetime(iso_timestamp)
    if dt is None:
        return iso_timestamp
    shifted = (dt - timedelta(minutes=minutes)).astimezone(timezone.utc)
    return shifted.strftime("%Y-%m-%dT%H:%M:%S.000Z")
