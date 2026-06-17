from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .models import AuctionItem
from .utils import stable_json, to_date, to_float, to_int

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  key TEXT PRIMARY KEY,
  auction_id TEXT NOT NULL,
  profile_name TEXT NOT NULL,
  score REAL NOT NULL,
  grade TEXT NOT NULL,
  address TEXT,
  usage TEXT,
  status TEXT,
  min_price INTEGER,
  appraisal_price INTEGER,
  failed_count INTEGER,
  land_area_m2 REAL,
  auction_date TEXT,
  detail_url TEXT,
  fingerprint TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  found_count INTEGER DEFAULT 0,
  new_count INTEGER DEFAULT 0,
  changed_count INTEGER DEFAULT 0,
  message TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS court_search_index (
    profile_key TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._remove_legacy_onbid_round_duplicates()

    @staticmethod
    def _profile_index_payload(profile: dict) -> dict:
        # 검색결과를 바꾸는 핵심 조건만 인덱스 키에 반영한다.
        keys = (
            "regions",
            "statuses",
            "usages",
            "failed_count",
            "min_price",
            "appraisal_price",
            "land_area_m2",
            "appraisal_discount_percent",
            "auction_within_days",
            "include_keywords",
            "exclude_keywords",
        )
        payload = {k: profile.get(k) for k in keys}
        payload["name"] = str(profile.get("name", ""))
        return payload

    @classmethod
    def _profile_index_key(cls, profile: dict) -> str:
        payload = cls._profile_index_payload(profile)
        digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
        return digest

    @staticmethod
    def _auction_item_from_dict(data: dict) -> AuctionItem:
        return AuctionItem(
            auction_id=str(data.get("auction_id", "")),
            sale_type=str(data.get("sale_type", "경매") or "경매"),
            source_name=str(data.get("source_name", "") or ""),
            case_number=str(data.get("case_number", "") or ""),
            item_number=str(data.get("item_number", "") or ""),
            court=str(data.get("court", "") or ""),
            status=str(data.get("status", "") or ""),
            usage=str(data.get("usage", "") or ""),
            address=str(data.get("address", "") or ""),
            province=str(data.get("province", "") or ""),
            city_county=str(data.get("city_county", "") or ""),
            min_price=to_int(data.get("min_price"), 0),
            appraisal_price=to_int(data.get("appraisal_price"), 0),
            failed_count=to_int(data.get("failed_count"), 0),
            land_area_m2=to_float(data.get("land_area_m2"), 0.0),
            building_area_m2=to_float(data.get("building_area_m2"), 0.0),
            auction_date=to_date(data.get("auction_date")),
            special_conditions=list(data.get("special_conditions") or []),
            detail_url=str(data.get("detail_url", "") or ""),
            market_estimate=to_int(data.get("market_estimate"), 0),
            nearby_avg_unit_price=to_float(data.get("nearby_avg_unit_price"), 0.0),
            raw=dict(data.get("raw") or {}),
            score=to_float(data.get("score"), 0.0),
            grade=str(data.get("grade", "미평가") or "미평가"),
            score_reasons=list(data.get("score_reasons") or []),
            risk_reasons=list(data.get("risk_reasons") or []),
            matched_profile=str(data.get("matched_profile", "") or ""),
        )

    def get_court_search_index(
        self,
        profile: dict,
        *,
        max_age_minutes: int,
    ) -> list[AuctionItem] | None:
        max_age_minutes = max(1, int(max_age_minutes or 1))
        key = self._profile_index_key(profile)
        row = self.conn.execute(
            "SELECT fetched_at, payload_json FROM court_search_index WHERE profile_key=?",
            (key,),
        ).fetchone()
        if not row:
            return None

        fetched_at = str(row["fetched_at"] or "")
        try:
            fetched_dt = datetime.fromisoformat(fetched_at)
        except ValueError:
            return None

        age_seconds = (datetime.now() - fetched_dt).total_seconds()
        if age_seconds > max_age_minutes * 60:
            return None

        try:
            payload = json.loads(row["payload_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, list):
            return None
        return [self._auction_item_from_dict(x) for x in payload if isinstance(x, dict)]

    def save_court_search_index(
        self,
        profile: dict,
        items: list[AuctionItem],
    ) -> None:
        key = self._profile_index_key(profile)
        profile_json = stable_json(self._profile_index_payload(profile))
        now = datetime.now().isoformat(timespec="seconds")
        payload_json = json.dumps([item.to_dict() for item in items], ensure_ascii=False, default=str)
        self.conn.execute(
            """
            INSERT INTO court_search_index(profile_key, profile_json, fetched_at, item_count, payload_json)
            VALUES(?,?,?,?,?)
            ON CONFLICT(profile_key) DO UPDATE SET
              profile_json=excluded.profile_json,
              fetched_at=excluded.fetched_at,
              item_count=excluded.item_count,
              payload_json=excluded.payload_json
            """,
            (key, profile_json, now, len(items), payload_json),
        )
        self.conn.commit()

    @staticmethod
    def _stored_onbid_rank(payload: dict, today: date | None = None) -> tuple:
        today = today or date.today()
        status = str(payload.get("status") or "")
        raw_date = str(payload.get("auction_date") or "").strip()
        try:
            auction_date = date.fromisoformat(raw_date[:10]) if raw_date else None
        except ValueError:
            auction_date = None
        terminal = any(word in status for word in ("낙찰", "취소", "종료", "마감", "매각완료"))
        future = auction_date is not None and auction_date >= today
        if future and not terminal:
            bucket = 0
        elif future:
            bucket = 1
        elif not terminal:
            bucket = 2
        else:
            bucket = 3
        distance = abs((auction_date - today).days) if auction_date else 999_999
        status_rank = 0 if ("진행" in status or "입찰" in status) else 1
        return (bucket, distance, status_rank, raw_date or "9999-12-31")

    def _remove_legacy_onbid_round_duplicates(self) -> int:
        """Collapse old DB rows that used round number in the Onbid primary key."""
        rows = self.conn.execute(
            "SELECT key, profile_name, payload_json FROM items"
        ).fetchall()
        groups: dict[tuple[str, str], list[tuple[sqlite3.Row, dict]]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if str(payload.get("sale_type") or "") != "공매":
                continue
            item_number = str(payload.get("item_number") or "").strip()
            if not item_number:
                continue
            groups.setdefault((str(row["profile_name"] or ""), item_number), []).append((row, payload))

        deleted = 0
        for entries in groups.values():
            if len(entries) <= 1:
                continue
            keep_row, _ = min(entries, key=lambda pair: self._stored_onbid_rank(pair[1]))
            delete_keys = [str(row["key"]) for row, _ in entries if row["key"] != keep_row["key"]]
            if delete_keys:
                self.conn.executemany("DELETE FROM items WHERE key=?", [(key,) for key in delete_keys])
                deleted += len(delete_keys)
        if deleted:
            self.conn.commit()
        return deleted

    def _remove_same_onbid_item(self, item: AuctionItem, keep_key: str) -> int:
        if str(item.sale_type or "") != "공매" or not str(item.item_number or "").strip():
            return 0
        rows = self.conn.execute(
            "SELECT key, payload_json FROM items WHERE profile_name=? AND key<>?",
            (item.matched_profile, keep_key),
        ).fetchall()
        delete_keys: list[str] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                str(payload.get("sale_type") or "") == "공매"
                and str(payload.get("item_number") or "").strip() == str(item.item_number).strip()
            ):
                delete_keys.append(str(row["key"]))
        if delete_keys:
            self.conn.executemany("DELETE FROM items WHERE key=?", [(key,) for key in delete_keys])
        return len(delete_keys)

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at,status) VALUES(?,?)",
            (datetime.now().isoformat(timespec="seconds"), "running"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, status: str, found: int, new: int, changed: int, message: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, found_count=?, new_count=?, changed_count=?, message=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), status, found, new, changed, message, run_id),
        )
        self.conn.commit()

    def upsert(self, item: AuctionItem) -> str:
        now = datetime.now().isoformat(timespec="seconds")
        key = f"{item.matched_profile}|{item.auction_id}"
        # Older versions keyed public-sale rows by pbctCdtnNo, leaving one DB row
        # per planned round. Remove those legacy siblings before the stable upsert.
        self._remove_same_onbid_item(item, key)
        payload = item.to_dict()
        # volatile fields excluded from fingerprint
        for k in ("raw", "score_reasons", "risk_reasons"):
            payload.pop(k, None)
        fingerprint = stable_json(payload)
        existing = self.conn.execute("SELECT fingerprint FROM items WHERE key=?", (key,)).fetchone()
        state = "new" if not existing else ("changed" if existing["fingerprint"] != fingerprint else "same")
        first_seen = now
        changed_at = now
        if existing:
            old = self.conn.execute("SELECT first_seen, changed_at FROM items WHERE key=?", (key,)).fetchone()
            first_seen = old["first_seen"]
            changed_at = now if state == "changed" else old["changed_at"]
        self.conn.execute(
            """
            INSERT INTO items(key,auction_id,profile_name,score,grade,address,usage,status,min_price,
              appraisal_price,failed_count,land_area_m2,auction_date,detail_url,fingerprint,
              first_seen,last_seen,changed_at,payload_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
              score=excluded.score, grade=excluded.grade, address=excluded.address,
              usage=excluded.usage, status=excluded.status, min_price=excluded.min_price,
              appraisal_price=excluded.appraisal_price, failed_count=excluded.failed_count,
              land_area_m2=excluded.land_area_m2, auction_date=excluded.auction_date,
              detail_url=excluded.detail_url, fingerprint=excluded.fingerprint,
              last_seen=excluded.last_seen, changed_at=excluded.changed_at,
              payload_json=excluded.payload_json
            """,
            (
                key, item.auction_id, item.matched_profile, item.score, item.grade, item.address,
                item.usage, item.status, item.min_price, item.appraisal_price, item.failed_count,
                item.land_area_m2, item.auction_date.isoformat() if item.auction_date else "",
                item.detail_url, fingerprint, first_seen, now, changed_at,
                json.dumps(item.to_dict(), ensure_ascii=False, default=str),
            ),
        )
        self.conn.commit()
        return state

    def recent_items(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM items ORDER BY score DESC, changed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_runs(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
