"""P2 팩트 장부 단위·통합 테스트 (DB 멱등 생성, 재검증 판정, Medium 필수 충돌 테스트, 시드 입력, 안전 가드)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from typing import Any

from kkultong_dash import (
    DEFAULT_COMPETITION_DATA_SOURCE_ID,
    DEFAULT_EXPERIENCE_DATA_SOURCE_ID,
    FACT_LEDGER_ACTORS,
    FACT_LEDGER_DB_TITLE,
    FACT_LEDGER_GRADES,
    FACT_LEDGER_REVALIDATION_RESULTS,
    KST,
    PARENT_PAGE_TITLE,
)
from kkultong_dash.cache import LocalCache
from kkultong_dash.fact_ledger import (
    PoliteWebFetcher,
    RawWebResponse,
    WebFetchResult,
    detect_fact_conflicts,
    ensure_fact_ledger_database,
    judge_revalidation,
    revalidate_fact_ledger,
)
from kkultong_dash.fact_seed import (
    DocumentUnit,
    extract_platform_seed_candidates,
    run_fact_seed,
)
from kkultong_dash.models import DashboardReport, FactLedgerRow, SourceSyncResult
from kkultong_dash.notion_client import HTTPResponse, NotionClient, ReadOnlyGuardError
from kkultong_dash.parsers import extract_block_plain_text
from kkultong_dash.renderer import build_top_blocks


class FakeFactNotionServer:
    """Pages, Blocks, Databases, Data Sources(2026-03-11) 인메모리 시뮬레이터."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[dict[str, Any]]] = {}
        self.databases: dict[str, dict[str, Any]] = {}
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.ds_rows: dict[str, list[dict[str, Any]]] = {}
        self.db_create_calls: int = 0
        self.page_update_calls: list[tuple[str, dict[str, Any]]] = []
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def add_page(
        self,
        page_id: str,
        title: str,
        *,
        parent_page_id: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        parent = (
            {"type": "page_id", "page_id": parent_page_id}
            if parent_page_id
            else {"type": "workspace", "workspace": True}
        )
        self.pages[page_id] = {
            "object": "page",
            "id": page_id,
            "in_trash": False,
            "parent": parent,
            "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
        }
        self.children[page_id] = list(blocks or [])
        if parent_page_id:
            self.children.setdefault(parent_page_id, []).append(
                {
                    "object": "block",
                    "id": page_id,
                    "type": "child_page",
                    "in_trash": False,
                    "child_page": {"title": title},
                }
            )

    def add_fact_row(
        self,
        ds_id: str,
        page_id: str,
        *,
        claim: str,
        topic_key: str,
        value: str,
        source_url: str | None,
        excerpt: str,
        grade: str,
        conflict_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        page = {
            "object": "page",
            "id": page_id,
            "in_trash": False,
            "is_archived": False,
            "parent": {"type": "data_source_id", "data_source_id": ds_id},
            "properties": {
                "주장": {"type": "title", "title": [{"plain_text": claim, "text": {"content": claim}}]},
                "주제키": {"type": "rich_text", "rich_text": [{"plain_text": topic_key, "text": {"content": topic_key}}]},
                "값": {"type": "rich_text", "rich_text": [{"plain_text": value, "text": {"content": value}}]},
                "근거 URL": {"type": "url", "url": source_url},
                "원문 발췌": {"type": "rich_text", "rich_text": [{"plain_text": excerpt, "text": {"content": excerpt}}]},
                "등급": {"type": "select", "select": {"name": grade}},
                "자동 재검증 시각": {"type": "date", "date": None},
                "자동 재검증 결과": {"type": "select", "select": None},
                "충돌": {"type": "relation", "relation": [{"id": cid} for cid in (conflict_ids or [])]},
                "대체됨": {"type": "relation", "relation": []},
            },
        }
        self.pages[page_id] = page
        self.ds_rows.setdefault(ds_id, []).append(page)
        return page

    def transport(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        payload = json.loads(body.decode("utf-8")) if body else {}
        path = url.split("/v1", 1)[1].split("?", 1)[0]

        if method == "POST" and path == "/search":
            q = payload.get("query", "")
            fval = (payload.get("filter") or {}).get("value")
            if fval == "data_source":
                matches = [
                    ds
                    for ds in self.data_sources.values()
                    if q in (ds.get("name") or "")
                ]
            else:
                matches = [
                    p
                    for p in self.pages.values()
                    if p.get("parent", {}).get("type") != "data_source_id"
                    and q in (p["properties"]["title"]["title"][0]["plain_text"])
                ]
            return HTTPResponse(200, {}, json.dumps({"object": "list", "results": matches, "has_more": False}))

        if method == "POST" and path == "/databases":
            self.db_create_calls += 1
            db_id = self._next_id("db")
            ds_id = self._next_id("ds")
            title_arr = payload.get("title") or []
            title_text = "".join(t.get("text", {}).get("content", "") for t in title_arr)
            parent = payload.get("parent") or {}
            init_props = (payload.get("initial_data_source") or {}).get("properties") or {}

            ds_obj = {
                "object": "data_source",
                "id": ds_id,
                "in_trash": False,
                "name": title_text,
                "title": [{"plain_text": title_text, "text": {"content": title_text}}],
                "parent": {"type": "database_id", "database_id": db_id},
                "database_parent": parent,
                "properties": dict(init_props),
            }
            db_obj = {
                "object": "database",
                "id": db_id,
                "in_trash": False,
                "parent": parent,
                "title": [{"plain_text": title_text, "text": {"content": title_text}}],
                "data_sources": [{"id": ds_id, "name": title_text}],
            }
            self.databases[db_id] = db_obj
            self.data_sources[ds_id] = ds_obj
            self.ds_rows.setdefault(ds_id, [])
            if parent.get("type") == "page_id":
                self.children.setdefault(parent["page_id"], []).append(
                    {
                        "object": "block",
                        "id": db_id,
                        "type": "child_database",
                        "in_trash": False,
                        "child_database": {"title": title_text},
                    }
                )
            return HTTPResponse(200, {}, json.dumps(db_obj))

        if method == "GET" and path.startswith("/databases/"):
            db_id = path.split("/databases/", 1)[1]
            if db_id not in self.databases:
                return HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "db not found"}))
            return HTTPResponse(200, {}, json.dumps(self.databases[db_id]))

        if method == "GET" and path.startswith("/data_sources/"):
            ds_id = path.split("/data_sources/", 1)[1]
            if ds_id not in self.data_sources:
                return HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "ds not found"}))
            return HTTPResponse(200, {}, json.dumps(self.data_sources[ds_id]))

        if method == "PATCH" and path.startswith("/data_sources/"):
            ds_id = path.split("/data_sources/", 1)[1]
            if ds_id not in self.data_sources:
                return HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "ds not found"}))
            ds = self.data_sources[ds_id]
            ds.setdefault("properties", {}).update(payload.get("properties") or {})
            return HTTPResponse(200, {}, json.dumps(ds))

        if method == "POST" and path.startswith("/data_sources/") and path.endswith("/query"):
            ds_id = path[len("/data_sources/") : -len("/query")]
            if ds_id not in self.data_sources:
                return HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "ds not found"}))
            rows = self.ds_rows.get(ds_id, [])
            return HTTPResponse(200, {}, json.dumps({"object": "list", "results": rows, "has_more": False}))

        if method == "GET" and path.startswith("/pages/"):
            pid = path.split("/pages/", 1)[1]
            if pid not in self.pages:
                return HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "page not found"}))
            return HTTPResponse(200, {}, json.dumps(self.pages[pid]))

        if method == "POST" and path == "/pages":
            parent = payload.get("parent") or {}
            if parent.get("type") == "data_source_id":
                ds_id = parent["data_source_id"]
                new_id = self._next_id("fact")
                props = payload.get("properties") or {}
                # 응답 정규화(plain_text 추가)
                norm_props = _normalize_written_props(props)
                page = {
                    "object": "page",
                    "id": new_id,
                    "in_trash": False,
                    "is_archived": False,
                    "parent": parent,
                    "properties": norm_props,
                }
                self.pages[new_id] = page
                self.ds_rows.setdefault(ds_id, []).append(page)
                return HTTPResponse(200, {}, json.dumps(page))
            return HTTPResponse(400, {}, json.dumps({"code": "validation_error", "message": "unsupported parent"}))

        if method == "PATCH" and path.startswith("/pages/"):
            pid = path.split("/pages/", 1)[1]
            if pid not in self.pages:
                return HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "page not found"}))
            written_props = payload.get("properties") or {}
            self.page_update_calls.append((pid, written_props))
            norm = _normalize_written_props(written_props)
            self.pages[pid]["properties"].update(norm)
            return HTTPResponse(200, {}, json.dumps(self.pages[pid]))

        if method == "GET" and path.startswith("/blocks/") and path.endswith("/children"):
            bid = path[len("/blocks/") : -len("/children")]
            items = [b for b in self.children.get(bid, []) if not b.get("in_trash")]
            return HTTPResponse(200, {}, json.dumps({"object": "list", "results": items, "has_more": False}))

        return HTTPResponse(404, {}, json.dumps({"code": "not_found", "message": f"unhandled {method} {path}"}))


def _normalize_written_props(props: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in props.items():
        if not isinstance(v, dict):
            continue
        if "title" in v:
            items = [
                {**it, "plain_text": it.get("text", {}).get("content", "")}
                for it in v["title"]
            ]
            out[k] = {"type": "title", "title": items}
        elif "rich_text" in v:
            items = [
                {**it, "plain_text": it.get("text", {}).get("content", "")}
                for it in v["rich_text"]
            ]
            out[k] = {"type": "rich_text", "rich_text": items}
        elif "select" in v:
            out[k] = {"type": "select", "select": v["select"]}
        elif "url" in v:
            out[k] = {"type": "url", "url": v["url"]}
        elif "date" in v:
            out[k] = {"type": "date", "date": v["date"]}
        elif "relation" in v:
            out[k] = {"type": "relation", "relation": v["relation"]}
    return out


class TestFactLedgerP2(unittest.TestCase):
    # ------------------------------------------------------------------
    # 1) DB 생성 멱등성 및 14개 속성 스키마 검증
    # ------------------------------------------------------------------

    def test_fact_ledger_db_creation_is_idempotent_and_has_exact_schema(self) -> None:
        server = FakeFactNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)

        with LocalCache(":memory:") as cache:
            r1 = ensure_fact_ledger_database(client, cache)
            self.assertTrue(r1.created)
            self.assertTrue(r1.relations_patched)
            self.assertEqual(server.db_create_calls, 1)

            ds = server.data_sources[r1.data_source_id]
            props = ds["properties"]
            expected_props = {
                "주장",
                "주제키",
                "값",
                "근거 URL",
                "원문 발췌",
                "확인 시각",
                "확인자",
                "등급",
                "자동 재검증 시각",
                "자동 재검증 결과",
                "충돌",
                "대체됨",
                "올린 이",
                "정정한 이",
            }
            self.assertEqual(set(props.keys()), expected_props)

            # select 옵션 검증
            self.assertEqual(
                tuple(o["name"] for o in props["확인자"]["select"]["options"]),
                FACT_LEDGER_ACTORS,
            )
            self.assertEqual(
                tuple(o["name"] for o in props["등급"]["select"]["options"]),
                FACT_LEDGER_GRADES,
            )
            self.assertEqual(
                tuple(o["name"] for o in props["자동 재검증 결과"]["select"]["options"]),
                FACT_LEDGER_REVALIDATION_RESULTS,
            )

            # 자기참조 relation 검증 (data_source_id == 자기 자신)
            self.assertEqual(props["충돌"]["relation"]["data_source_id"], r1.data_source_id)
            self.assertEqual(props["대체됨"]["relation"]["data_source_id"], r1.data_source_id)

            # 2회차 호출: 캐시 히트 -> 재생성 금지
            r2 = ensure_fact_ledger_database(client, cache)
            self.assertFalse(r2.created)
            self.assertEqual(r2.database_id, r1.database_id)
            self.assertEqual(r2.data_source_id, r1.data_source_id)
            self.assertEqual(server.db_create_calls, 1)

        # 새로운 빈 SQLite 캐시에서도 부모 페이지 하위 child_database 스캔으로 기존 DB 발견 -> 재생성 금지
        with LocalCache(":memory:") as fresh_cache:
            r3 = ensure_fact_ledger_database(client, fresh_cache)
            self.assertFalse(r3.created)
            self.assertEqual(r3.database_id, r1.database_id)
            self.assertEqual(server.db_create_calls, 1)

    # ------------------------------------------------------------------
    # 2) 필수 합격 테스트 (요구사항 5): Medium MPP 멤버십 충돌 판정
    # ------------------------------------------------------------------

    def test_required_medium_mpp_membership_conflict_downgrades_and_links_both_rows(self) -> None:
        url_new = "https://help.medium.com/hc/en-us/articles/39121627791639"
        url_old = "https://help.medium.com/hc/en-us/articles/115011694187"

        # (A) 순수 충돌 판정 함수 단위 검증
        row_a = FactLedgerRow(
            page_id="fact-medium-new",
            claim="Medium MPP 가입 시 유료 멤버십 불필요",
            topic_key="medium.mpp.membership_required",
            value="멤버십 불필요",
            source_url=url_new,
            excerpt="You no longer need to be a Medium member to join the Partner Program.",
            grade="확인됨",
        )
        row_b = FactLedgerRow(
            page_id="fact-medium-old",
            claim="Medium MPP 가입 시 유료 멤버십 필수",
            topic_key="medium.mpp.membership_required",
            value="멤버십 필수",
            source_url=url_old,
            excerpt="To join the Partner Program, you must be a Medium member.",
            grade="확인됨",
        )

        conflicts = detect_fact_conflicts([row_a, row_b])
        self.assertEqual(len(conflicts), 2)
        by_id = {c.page_id: c for c in conflicts}
        self.assertEqual(by_id["fact-medium-new"].new_grade, "불일치")
        self.assertEqual(by_id["fact-medium-old"].new_grade, "불일치")
        self.assertEqual(by_id["fact-medium-new"].conflict_ids, ("fact-medium-old",))
        self.assertEqual(by_id["fact-medium-old"].conflict_ids, ("fact-medium-new",))

        # (B) 모의 노션 DB + 픽스처 웹 응답을 사용한 전체 재검증 파이프라인 통합 검증
        server = FakeFactNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)

        fixtures = {
            "https://help.medium.com/robots.txt": RawWebResponse(200, "https://help.medium.com/robots.txt", {}, "User-agent: *\nAllow: /\n"),
            url_new: RawWebResponse(
                200,
                url_new,
                {},
                "<html><body><p>You no longer need to be a Medium member to join the Partner Program.</p></body></html>",
            ),
            url_old: RawWebResponse(
                200,
                url_old,
                {},
                "<html><body><p>To join the Partner Program, you must be a Medium member.</p></body></html>",
            ),
        }
        fetcher = PoliteWebFetcher(
            host_delay_sec=0.0,
            transport=lambda u, h, t: fixtures[u],
            sleep_fn=lambda _: None,
        )

        with LocalCache(":memory:") as cache:
            init_res = ensure_fact_ledger_database(client, cache)
            ds_id = init_res.data_source_id
            server.add_fact_row(
                ds_id,
                "fact-medium-new",
                claim=row_a.claim,
                topic_key=row_a.topic_key,
                value=row_a.value,
                source_url=url_new,
                excerpt=row_a.excerpt,
                grade="확인됨",
            )
            server.add_fact_row(
                ds_id,
                "fact-medium-old",
                claim=row_b.claim,
                topic_key=row_b.topic_key,
                value=row_b.value,
                source_url=url_old,
                excerpt=row_b.excerpt,
                grade="확인됨",
            )

            outcome = revalidate_fact_ledger(
                client,
                cache,
                now_kst=datetime(2026, 11, 21, 5, 0, 0, tzinfo=KST),
                fetcher=fetcher,
            )
            self.assertEqual(len(outcome.conflicts), 2)

            p_new = server.pages["fact-medium-new"]["properties"]
            p_old = server.pages["fact-medium-old"]["properties"]
            self.assertEqual(p_new["등급"]["select"]["name"], "불일치")
            self.assertEqual(p_old["등급"]["select"]["name"], "불일치")
            self.assertEqual(p_new["충돌"]["relation"], [{"id": "fact-medium-old"}])
            self.assertEqual(p_old["충돌"]["relation"], [{"id": "fact-medium-new"}])
            self.assertEqual(p_new["자동 재검증 결과"]["select"]["name"], "발췌 그대로")
            self.assertEqual(p_old["자동 재검증 결과"]["select"]["name"], "발췌 그대로")

    # ------------------------------------------------------------------
    # 3) 재검증 판정 단위 테스트 (발췌 그대로 / 발췌 사라짐 / 로그인 필요 / 페이지 열림 실패)
    # ------------------------------------------------------------------

    def test_revalidation_judgements_excerpt_present_absent_login_and_failure(self) -> None:
        # 3-1) 발췌 그대로: 확인됨 유지, 원문대조필요는 절대 확인됨으로 상향하지 않음!
        confirmed_row = FactLedgerRow(
            page_id="f1",
            claim="Gumroad 수수료 10%",
            topic_key="gumroad.fee",
            value="10%",
            source_url="https://gumroad.example/pricing",
            excerpt="Gumroad charges a flat 10% fee.",
            grade="확인됨",
        )
        needs_check_row = FactLedgerRow(
            page_id="f2",
            claim="Substack 수수료 10%",
            topic_key="substack.fee",
            value="10%",
            source_url="https://substack.example/pricing",
            excerpt="Substack takes a 10% cut.",
            grade="원문대조필요",
        )

        ok_present = WebFetchResult(
            kind="ok",
            requested_url="https://gumroad.example/pricing",
            final_url="https://gumroad.example/pricing",
            status_code=200,
            visible_text="Pricing overview: Gumroad charges a flat 10% fee. No monthly fee.",
        )
        d_present_confirmed = judge_revalidation(confirmed_row, ok_present)
        self.assertEqual(d_present_confirmed.result, "발췌 그대로")
        self.assertEqual(d_present_confirmed.new_grade, "확인됨")
        self.assertFalse(d_present_confirmed.add_to_fact_alerts)

        ok_present_sub = WebFetchResult(
            kind="ok",
            requested_url="https://substack.example/pricing",
            final_url="https://substack.example/pricing",
            status_code=200,
            visible_text="Substack takes a 10% cut.",
        )
        d_present_needs = judge_revalidation(needs_check_row, ok_present_sub)
        self.assertEqual(d_present_needs.result, "발췌 그대로")
        self.assertEqual(d_present_needs.new_grade, "원문대조필요")  # 절대 '확인됨'으로 상향 금지!

        # 3-2) 발췌 사라짐: 확인됨 -> 원문대조필요 하향 + '팩트 경보' 추가
        ok_absent = WebFetchResult(
            kind="ok",
            requested_url="https://gumroad.example/pricing",
            final_url="https://gumroad.example/pricing",
            status_code=200,
            visible_text="Pricing overview: Updated fee is now 15%.",
        )
        d_absent = judge_revalidation(confirmed_row, ok_absent)
        self.assertEqual(d_absent.result, "발췌 사라짐")
        self.assertEqual(d_absent.new_grade, "원문대조필요")
        self.assertTrue(d_absent.add_to_fact_alerts)

        # 3-3) 로그인 필요 (HTTP 401/리다이렉트 또는 로그인 벽) -> '자동확인불가' + 재시도 제외 목록 등록
        login_res = WebFetchResult(
            kind="login_required",
            requested_url="https://member.example/doc",
            final_url="https://member.example/login",
            status_code=200,
            detail="로그인 페이지로 리다이렉트됨",
        )
        d_login = judge_revalidation(confirmed_row, login_res)
        self.assertEqual(d_login.result, "로그인 필요")
        self.assertEqual(d_login.new_grade, "자동확인불가")
        self.assertTrue(d_login.add_to_skip_list)
        self.assertFalse(d_login.add_to_fact_alerts)

        login_wall_res = WebFetchResult(
            kind="ok",
            requested_url="https://member.example/post",
            final_url="https://member.example/post",
            status_code=200,
            visible_text="로그인이 필요합니다. 계속하려면 로그인해 주세요.",
            has_login_wall=True,
        )
        d_login_wall = judge_revalidation(confirmed_row, login_wall_res)
        self.assertEqual(d_login_wall.result, "로그인 필요")
        self.assertEqual(d_login_wall.new_grade, "자동확인불가")
        self.assertTrue(d_login_wall.add_to_skip_list)

        # 3-4) 페이지 열림 실패 -> 결과에 그대로 기록, 등급 불변
        fail_res = WebFetchResult(
            kind="open_failed",
            requested_url="https://down.example/page",
            final_url="https://down.example/page",
            status_code=503,
            detail="HTTP 503 오류",
        )
        d_fail = judge_revalidation(confirmed_row, fail_res)
        self.assertEqual(d_fail.result, "페이지 열림 실패")
        self.assertEqual(d_fail.new_grade, "확인됨")
        self.assertFalse(d_fail.add_to_fact_alerts)

    def test_revalidation_missing_excerpt_alerts_section_a_and_login_never_retries(self) -> None:
        server = FakeFactNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)

        fetch_calls: list[str] = []
        fixtures = {
            "https://docs.example/robots.txt": RawWebResponse(200, "https://docs.example/robots.txt", {}, "User-agent: *\nAllow: /\n"),
            "https://docs.example/changed": RawWebResponse(
                200,
                "https://docs.example/changed",
                {},
                "<html><body><p>Fee changed to 12%.</p></body></html>",
            ),
            "https://docs.example/private": RawWebResponse(
                200,
                "https://docs.example/login?next=/private",
                {},
                "<html><body><form><input type='password' name='pw'/></form>Sign in to continue</body></html>",
            ),
        }

        def fixture_transport(url: str, headers: dict[str, str], timeout: float) -> RawWebResponse:
            fetch_calls.append(url)
            return fixtures[url]

        fetcher = PoliteWebFetcher(host_delay_sec=0.0, transport=fixture_transport, sleep_fn=lambda _: None)

        with LocalCache(":memory:") as cache:
            init_res = ensure_fact_ledger_database(client, cache)
            ds_id = init_res.data_source_id

            server.add_fact_row(
                ds_id,
                "fact-changed",
                claim="수수료 10%",
                topic_key="example.fee",
                value="10%",
                source_url="https://docs.example/changed",
                excerpt="Fee is 10%.",
                grade="확인됨",
            )
            server.add_fact_row(
                ds_id,
                "fact-private",
                claim="비공개 정산표",
                topic_key="example.payout",
                value="매월 15일",
                source_url="https://docs.example/private",
                excerpt="Paid on the 15th",
                grade="확인됨",
            )

            out1 = revalidate_fact_ledger(
                client,
                cache,
                now_kst=datetime(2026, 11, 21, 5, 0, 0, tzinfo=KST),
                fetcher=fetcher,
            )
            self.assertEqual(server.pages["fact-changed"]["properties"]["등급"]["select"]["name"], "원문대조필요")
            self.assertEqual(server.pages["fact-changed"]["properties"]["자동 재검증 결과"]["select"]["name"], "발췌 사라짐")
            self.assertEqual(server.pages["fact-private"]["properties"]["등급"]["select"]["name"], "자동확인불가")
            self.assertEqual(server.pages["fact-private"]["properties"]["자동 재검증 결과"]["select"]["name"], "로그인 필요")

            # 로그인 필요 URL은 재시도 금지 목록에 기록됨
            self.assertTrue(cache.is_url_in_skip_list("https://docs.example/private"))

            # '팩트 경보'가 SQLite에 저장되고 대시보드 섹션 A에 렌더링됨
            saved_alerts = cache.load_fact_alerts()
            self.assertEqual(len(saved_alerts), 1)
            self.assertEqual(saved_alerts[0].topic_key, "example.fee")

            report = DashboardReport(
                updated_at_kst=datetime(2026, 11, 21, 11, 20, 0, tzinfo=KST),
                run_status="정상",
                failure_reasons=[],
                warnings=[],
                experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 1, 1),
                competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 1, 1),
                experience_analytics=None,
                competition_analytics=None,
                fact_alerts=saved_alerts,
            )
            top_blocks = build_top_blocks(report)
            texts = [extract_block_plain_text(b) for b in top_blocks]
            self.assertIn("팩트 경보", texts)
            idx_a = texts.index("A. 실행 상태 (머리줄)")
            idx_alert = texts.index("팩트 경보")
            idx_b = texts.index("B. 마감 임박")
            self.assertTrue(idx_a < idx_alert < idx_b)

            # 2회차 실행: 다른 행이 같은 로그인 URL을 '원문대조필요'로 갖고 있어도 절대 재요청하지 않음!
            server.add_fact_row(
                ds_id,
                "fact-private-dup",
                claim="비공개 정산표 2",
                topic_key="example.payout2",
                value="매월 20일",
                source_url="https://docs.example/private",
                excerpt="Paid on the 20th",
                grade="원문대조필요",
            )
            fetch_calls.clear()
            out2 = revalidate_fact_ledger(
                client,
                cache,
                now_kst=datetime(2026, 11, 22, 5, 0, 0, tzinfo=KST),
                fetcher=fetcher,
            )
            self.assertNotIn("https://docs.example/private", fetch_calls)
            self.assertEqual(out2.skipped_login_count, 1)
            self.assertEqual(server.pages["fact-private-dup"]["properties"]["등급"]["select"]["name"], "자동확인불가")

    def test_polite_fetcher_respects_robots_txt_and_same_host_delay(self) -> None:
        calls: list[str] = []
        sleeps: list[float] = []
        now = [100.0]

        def fake_monotonic() -> float:
            return now[0]

        def fake_sleep(sec: float) -> None:
            sleeps.append(sec)
            now[0] += sec

        fixtures = {
            "https://polite.example/robots.txt": RawWebResponse(
                200,
                "https://polite.example/robots.txt",
                {},
                "User-agent: *\nDisallow: /secret\nCrawl-delay: 5\n",
            ),
            "https://polite.example/page1": RawWebResponse(200, "https://polite.example/page1", {}, "<p>hello 1</p>"),
            "https://polite.example/page2": RawWebResponse(200, "https://polite.example/page2", {}, "<p>hello 2</p>"),
        }

        def transport(url: str, headers: dict[str, str], timeout: float) -> RawWebResponse:
            calls.append(url)
            return fixtures[url]

        fetcher = PoliteWebFetcher(
            host_delay_sec=3.0,
            transport=transport,
            sleep_fn=fake_sleep,
            monotonic_fn=fake_monotonic,
        )

        # robots.txt 차단 경로는 본 요청 자체가 나가지 않아야 함
        blocked = fetcher.fetch("https://polite.example/secret/item")
        self.assertEqual(blocked.kind, "robots_blocked")
        self.assertNotIn("https://polite.example/secret/item", calls)

        # 동일 호스트 연속 요청 시 Crawl-delay(5초) 준수 확인
        r1 = fetcher.fetch("https://polite.example/page1")
        r2 = fetcher.fetch("https://polite.example/page2")
        self.assertEqual(r1.kind, "ok")
        self.assertEqual(r2.kind, "ok")
        self.assertEqual(sleeps, [5.0])

    # ------------------------------------------------------------------
    # 4) 안전 가드: 기존 DB 쓰기 차단 + 등급 '확인됨' 쓰기 원천 차단
    # ------------------------------------------------------------------

    def test_guards_block_existing_db_writes_and_confirmed_grade_upgrades(self) -> None:
        server = FakeFactNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        client = NotionClient(
            "secret_test",
            min_interval_sec=0.0,
            protected_ids=[DEFAULT_EXPERIENCE_DATA_SOURCE_ID, DEFAULT_COMPETITION_DATA_SOURCE_ID],
            transport=server.transport,
        )

        # 보호된 기존 DB는 팩트 장부로 등록조차 불가
        with self.assertRaises(ReadOnlyGuardError):
            client.register_fact_ledger_data_source(DEFAULT_EXPERIENCE_DATA_SOURCE_ID)
        with self.assertRaises(ReadOnlyGuardError):
            client.create_fact_ledger_page(DEFAULT_COMPETITION_DATA_SOURCE_ID, {})

        # 허용된 팩트 장부 DB라도 등급 '확인됨' 쓰기는 무조건 차단
        with LocalCache(":memory:") as cache:
            init_res = ensure_fact_ledger_database(client, cache)
            with self.assertRaises(ReadOnlyGuardError):
                client.create_fact_ledger_page(
                    init_res.data_source_id,
                    {"등급": {"select": {"name": "확인됨"}}},
                )
            with self.assertRaises(ReadOnlyGuardError):
                client.update_fact_ledger_page(
                    "some-fact-page",
                    {"등급": {"select": {"name": "확인됨"}}},
                    data_source_id=init_res.data_source_id,
                )

    # ------------------------------------------------------------------
    # 5) 시드 입력 (요구사항 4): v1/v2 추출·변환 + v2 근거 URL 전용 + 접근 실패 처리
    # ------------------------------------------------------------------

    def test_seed_extraction_uses_only_v2_urls_and_marks_unreferenced_as_unverified(self) -> None:
        v1_units = [
            DocumentUnit(kind="heading", text="플랫폼 수수료 초안 (v1)"),
            # v1에만 적힌 URL은 절대 근거 URL로 사용되면 안 됨!
            DocumentUnit(
                kind="text",
                text="Gumroad: 직접 판매 수수료 10% + $0.50 (https://v1-only.gumroad.com/should-not-be-used)",
                heading_context="플랫폼 수수료 초안 (v1)",
                urls=("https://v1-only.gumroad.com/should-not-be-used",),
            ),
            DocumentUnit(
                kind="text",
                text="Substack: 유료 구독 수수료 10%",
                heading_context="플랫폼 수수료 초안 (v1)",
                urls=(),
            ),
        ]
        v2_units = [
            DocumentUnit(kind="heading", text="플랫폼별 수치 및 근거 (v2)"),
            DocumentUnit(
                kind="table_row",
                text="Patreon | 플랫폼 수수료 10% | https://support.patreon.com/hc/en-us/articles/fees",
                heading_context="플랫폼별 수치 및 근거 (v2)",
                table_headers=("플랫폼", "수수료", "근거"),
                table_cells=("Patreon", "플랫폼 수수료 10%", "https://support.patreon.com/hc/en-us/articles/fees"),
                urls=("https://support.patreon.com/hc/en-us/articles/fees",),
            ),
            DocumentUnit(
                kind="text",
                text="Medium: 월 $5 멤버십 기준 (https://help.medium.com/hc/en-us/articles/39121627791639)",
                heading_context="플랫폼별 수치 및 근거 (v2)",
                urls=("https://help.medium.com/hc/en-us/articles/39121627791639",),
            ),
            DocumentUnit(
                kind="text",
                text="네이버 프리미엄콘텐츠: 판매 수수료 10% (근거 URL 미기재)",
                heading_context="플랫폼별 수치 및 근거 (v2)",
                urls=(),
            ),
        ]

        candidates = extract_platform_seed_candidates(v1_units, v2_units, uploader="뮤즈")
        by_platform = {c.platform: c for c in candidates}

        self.assertEqual(
            set(by_platform.keys()),
            {"Gumroad", "Patreon", "Medium", "네이버 프리미엄콘텐츠", "Substack"},
        )

        # v1에만 있던 URL은 절대 쓰이지 않고 등급 '미확인'이어야 함
        self.assertIsNone(by_platform["Gumroad"].source_url)
        self.assertEqual(by_platform["Gumroad"].grade, "미확인")

        # v2에 근거 URL이 없는 네이버 프리미엄콘텐츠, Substack도 등급 '미확인'
        self.assertIsNone(by_platform["네이버 프리미엄콘텐츠"].source_url)
        self.assertEqual(by_platform["네이버 프리미엄콘텐츠"].grade, "미확인")
        self.assertIsNone(by_platform["Substack"].source_url)
        self.assertEqual(by_platform["Substack"].grade, "미확인")

        # v2에 근거 URL이 있는 Patreon, Medium은 v2 URL 사용 + 등급 '원문대조필요' (절대 '확인됨' 금지!)
        self.assertEqual(by_platform["Patreon"].source_url, "https://support.patreon.com/hc/en-us/articles/fees")
        self.assertEqual(by_platform["Patreon"].grade, "원문대조필요")
        self.assertEqual(by_platform["Medium"].source_url, "https://help.medium.com/hc/en-us/articles/39121627791639")
        self.assertEqual(by_platform["Medium"].grade, "원문대조필요")

        # 어떤 후보도 '확인됨'이면 안 됨
        self.assertTrue(all(c.grade != "확인됨" for c in candidates))

    def test_seed_inaccessible_page_reports_failure_without_guessing_rows(self) -> None:
        server = FakeFactNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        # v1만 존재하고 v2는 공유되지 않아 검색/조회 불가 상황
        server.add_page("seed-v1-id", "C 갈래 영어 시범 — 작업 분할 v1", parent_page_id="hub-parent-1")

        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)
        with LocalCache(":memory:") as cache:
            outcome = run_fact_seed(
                client,
                cache,
                v1_identifier="C 갈래 영어 시범 — 작업 분할 v1",
                v2_identifier="원소스 멀티유즈 워크플로우 v2",
                dry_run=False,
            )
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.candidates, [])
            self.assertEqual(outcome.created_count, 0)
            self.assertIn("원소스 멀티유즈 워크플로우 v2", outcome.error_message or "")
            self.assertEqual(server.db_create_calls, 0)  # 실패 시 DB도 건드리지 않음

    def test_seed_end_to_end_creates_rows_and_deduplicates_on_rerun(self) -> None:
        server = FakeFactNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        server.add_page(
            "11111111-1111-1111-1111-111111111111",
            "C 갈래 영어 시범 — 작업 분할 v1",
            parent_page_id="hub-parent-1",
            blocks=[
                {
                    "object": "block",
                    "id": "b1",
                    "type": "bulleted_list_item",
                    "in_trash": False,
                    "bulleted_list_item": {
                        "rich_text": [{"plain_text": "Substack 수수료: 10%"}]
                    },
                }
            ],
        )
        server.add_page(
            "22222222-2222-2222-2222-222222222222",
            "원소스 멀티유즈 워크플로우 v2",
            parent_page_id="hub-parent-1",
            blocks=[
                {
                    "object": "block",
                    "id": "b2",
                    "type": "bulleted_list_item",
                    "in_trash": False,
                    "bulleted_list_item": {
                        "rich_text": [
                            {
                                "plain_text": "Gumroad 수수료: 10% + $0.50",
                                "href": "https://gumroad.com/pricing",
                            }
                        ]
                    },
                }
            ],
        )

        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)
        with LocalCache(":memory:") as cache:
            out1 = run_fact_seed(
                client,
                cache,
                v1_identifier="11111111-1111-1111-1111-111111111111",
                v2_identifier="22222222-2222-2222-2222-222222222222",
                uploader="뮤즈",
            )
            self.assertTrue(out1.ok)
            self.assertEqual(out1.created_count, 2)
            self.assertEqual(out1.skipped_existing_count, 0)

            # 재실행 시 기존 행 중복 생성 방지
            out2 = run_fact_seed(
                client,
                cache,
                v1_identifier="11111111-1111-1111-1111-111111111111",
                v2_identifier="22222222-2222-2222-2222-222222222222",
                uploader="뮤즈",
            )
            self.assertTrue(out2.ok)
            self.assertEqual(out2.created_count, 0)
            self.assertEqual(out2.skipped_existing_count, 2)


if __name__ == "__main__":
    unittest.main()
