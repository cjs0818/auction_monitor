from __future__ import annotations

import html
import smtplib
from email.mime.text import MIMEText
from typing import Any

import requests

from .models import AuctionItem


def build_message(items: list[AuctionItem], title: str = "토지 경매·공매 신규 후보") -> str:
    lines = [f"[{title}] {len(items)}건"]
    for i, item in enumerate(items[:20], 1):
        number_label = "공고" if item.sale_type == "공매" else "사건"
        item_label = "물건관리번호" if item.sale_type == "공매" else "물건번호"
        price_label = "최저입찰가" if item.sale_type == "공매" else "최저매각가격"
        date_label = "입찰마감일" if item.sale_type == "공매" else "매각기일"
        lines.append(
            f"\n{i}. [{item.sale_type}] {item.grade} {item.score:.1f}점 | {item.usage} | {item.address}\n"
            f"   {number_label} {item.case_number or '-'} / {item_label} {item.item_number or '-'}\n"
            f"   {price_label} {item.min_price:,}원 / 감정평가액 {item.appraisal_price:,}원 / "
            f"유찰횟수 {item.failed_count}회 / 토지면적 {item.land_area_m2:,.0f}㎡\n"
            f"   {date_label}: {item.auction_date.isoformat() if item.auction_date else '-'}\n"
            f"   검토근거: {', '.join(item.score_reasons) or '추가 확인'}\n"
            f"   주의사항: {', '.join(item.risk_reasons) or '자동 탐지 없음'}\n"
            f"   {item.detail_url}"
        )
    return "\n".join(lines)


def send_notifications(items: list[AuctionItem], cfg: dict[str, Any]) -> list[str]:
    if not items:
        return []
    results: list[str] = []
    text = build_message(items)

    tg = cfg.get("telegram", {}) or {}
    if tg.get("enabled"):
        token, chat_id = tg.get("bot_token"), tg.get("chat_id")
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            resp = requests.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=20)
            resp.raise_for_status()
            results.append("telegram")

    em = cfg.get("email", {}) or {}
    if em.get("enabled"):
        recipients = [x for x in em.get("to_addresses", []) if x]
        if recipients:
            body = "<pre style='white-space:pre-wrap'>" + html.escape(text) + "</pre>"
            msg = MIMEText(body, "html", "utf-8")
            msg["Subject"] = "[LandWatch] 토지 경매·공매 신규/변경 후보"
            msg["From"] = em.get("from_address") or em.get("username")
            msg["To"] = ", ".join(recipients)
            with smtplib.SMTP(em.get("smtp_host"), int(em.get("smtp_port", 587))) as s:
                s.starttls()
                s.login(em.get("username"), em.get("password"))
                s.sendmail(msg["From"], recipients, msg.as_string())
            results.append("email")
    return results
