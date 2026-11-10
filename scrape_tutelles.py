import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

URL = "https://www.minesup.gov.cm/index.php/missions-du-minesup/"
SITE_MINESUP = "https://www.minesup.gov.cm/"
OUTPUT = Path("json_exports/tutelles.json")


def write_json(items, evidence):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "table": "tutelleInstitutionelle",
        "status": "pending_admin_review",
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "sourceUrls": [URL],
        "count": len(items),
        "items": items,
        "sourceEvidence": evidence,
        "notes": [
            "Le fichier décrit MINESUP, pas la tutelle individuelle de chaque établissement.",
            "Les IDs Prisma et paysId restent nuls.",
        ],
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Fichier créé : {OUTPUT.resolve()}")


async def collect():
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=60000,
        check_robots_txt=True,
    )
    async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
        result = await crawler.arun(url=URL, config=config)

    if not result.success:
        raise RuntimeError(f"Échec du crawl de {URL}: {result.error_message}")

    soup = BeautifulSoup(result.html or "", "html.parser")
    paragraphs = [
        re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
        for tag in soup.select("main p, article p, .entry-content p, p")
    ]

    evidence = [
        text[:350]
        for text in paragraphs
        if "tutelle" in text.casefold()
        or "accréditation" in text.casefold()
        or "accreditation" in text.casefold()
    ][:5]

    item = {
        "id": None,
        "nomTutelle": "Ministère de l'Enseignement Supérieur",
        "nomTutelleTech": "MINESUP",
        "tutelleBio": None,
        "siteTutelle": SITE_MINESUP,
        "logoTutelle": None,
        "paysId": None,
        "paysSource": "Cameroun",
        "sourceUrl": URL,
    }
    return [item], evidence


if __name__ == "__main__":
    records, evidence = asyncio.run(collect())

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "table": "tutelleInstitutionelle",
        "status": "pending_admin_review",
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "sourceUrls": [URL],
        "count": len(records),
        "items": records,
        "sourceEvidence": evidence,
        "notes": [
            "Le fichier décrit MINESUP, pas la tutelle individuelle de chaque établissement.",
            "Les IDs Prisma et paysId restent nuls.",
        ],
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Fichier créé : {OUTPUT.resolve()}")