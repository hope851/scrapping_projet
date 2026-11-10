import asyncio
import json
import os
import re
from datetime import datetime, timezone, date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    LLMConfig,
    LLMExtractionStrategy,
)
from crawl4ai.processors.pdf import PDFCrawlerStrategy, PDFContentScrapingStrategy

OUTPUT = Path("json_exports/sessions_admission.json")
ARCHIVE_URL = "https://www.minesup.gov.cm/index.php/category/concours/"
MAX_ARCHIVE_PAGES = int(os.getenv("MAX_ARCHIVE_PAGES", "6"))
MAX_DOCUMENTS = int(os.getenv("MAX_CONCOURS_DOCUMENTS", "100"))

SCHEMA = {
    "title": "SessionsAdmission",
    "type": "object",
    "properties": {
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nomEes": {"type": "string"},
                    "nomConcours": {"type": "string"},
                    "programmeNom": {"type": ["string", "null"]},
                    "dateOuverture": {"type": ["string", "null"]},
                    "limiteDepot": {"type": ["string", "null"]},
                    "dateExamEcrit": {"type": ["string", "null"]},
                    "dateExamOral": {"type": ["string", "null"]},
                    "diplomeRequis": {"type": ["string", "null"]},
                    "adressExam": {"type": ["string", "null"]},
                    "concoursBio": {"type": ["string", "null"]},
                    "preuve": {"type": "string"},
                },
                "required": [
                    "nomEes",
                    "nomConcours",
                    "programmeNom",
                    "dateOuverture",
                    "limiteDepot",
                    "dateExamEcrit",
                    "dateExamOral",
                    "diplomeRequis",
                    "adressExam",
                    "concoursBio",
                    "preuve",
                ],
            },
        }
    },
    "required": ["sessions"],
}

INSTRUCTION = """
Extrais les concours et admissions explicitement décrits dans cette page ou
ce PDF du MINESUP. N'invente aucune date ou condition. Renvoie les dates au
format YYYY-MM-DD uniquement si le document contient une date complète avec
l'année ; sinon renvoie null. Donne le nom de l'établissement, le concours,
le programme s'il est nommé, les dates, le diplôme requis, l'adresse d'examen,
une brève description et une courte preuve textuelle.
"""


def norm(text):
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def get_markdown(result):
    value = result.markdown or ""
    if isinstance(value, str):
        return value
    return (
        getattr(value, "raw_markdown", None)
        or getattr(value, "fit_markdown", None)
        or str(value)
    )


def parse_extraction(result):
    raw = result.extracted_content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def evidence_found(evidence, source_text):
    return bool(evidence) and norm(evidence) in norm(source_text)


def make_strategy():
    provider = os.getenv("LLM_PROVIDER", "openai/gpt-4o-mini")
    token = os.getenv("LLM_API_TOKEN") or os.getenv("OPENAI_API_KEY")
    if provider.startswith("openai/") and not token:
        raise RuntimeError("Définis OPENAI_API_KEY pour l'extraction des concours.")

    return LLMExtractionStrategy(
        llm_config=LLMConfig(provider=provider, api_token=token),
        schema=SCHEMA,
        extraction_type="schema",
        instruction=INSTRUCTION,
        input_format="markdown",
        apply_chunking=True,
        chunk_token_threshold=6000,
        extra_args={"temperature": 0},
    )


def is_minesup(url):
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith("minesup.gov.cm")


def is_pdf(url):
    return ".pdf" in urlsplit(url).path.lower()


async def discover_documents(html_crawler):
    archive_queue = [ARCHIVE_URL]
    visited_archives = set()
    articles = []
    pdfs = []

    while archive_queue and len(visited_archives) < MAX_ARCHIVE_PAGES:
        archive_url = archive_queue.pop(0)
        if archive_url in visited_archives:
            continue
        visited_archives.add(archive_url)

        config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=60000,
            check_robots_txt=True,
        )
        result = await html_crawler.arun(url=archive_url, config=config)
        if not result.success:
            continue

        soup = BeautifulSoup(result.html or "", "html.parser")

        for anchor in soup.select("a[href]"):
            label = anchor.get_text(" ", strip=True)
            target = urljoin(archive_url, anchor.get("href", "")).split("#", 1)[0]

            if not is_minesup(target):
                continue

            if is_pdf(target):
                pdfs.append(target)
            elif re.search(r"page suivante|older posts|suivant", label, re.I):
                if target not in visited_archives:
                    archive_queue.append(target)

        selectors = (
            "article h2 a[href], article h3 a[href], "
            "h2.entry-title a[href], h3.entry-title a[href], "
            ".entry-title a[href]"
        )
        for anchor in soup.select(selectors):
            target = urljoin(archive_url, anchor.get("href", "")).split("#", 1)[0]
            if is_minesup(target) and not is_pdf(target):
                articles.append(target)

    return list(dict.fromkeys(articles)), list(dict.fromkeys(pdfs))


async def extract_from_url(crawler, url, is_pdf=False):
    strategy = make_strategy()
    args = {
        "cache_mode": CacheMode.BYPASS,
        "page_timeout": 60000,
        "check_robots_txt": True,
        "extraction_strategy": strategy,
    }
    if is_pdf:
        args["scraping_strategy"] = PDFContentScrapingStrategy()

    result = await crawler.arun(
        url=url,
        config=CrawlerRunConfig(**args),
    )
    if not result.success:
        return {}, ""
    return parse_extraction(result), get_markdown(result)


def iso_date(value):
    if not value:
        return None
    value = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        date.fromisoformat(value)
        return value
    except ValueError:
        return None


def make_record(row, url, evidence):
    opening = iso_date(row.get("dateOuverture"))
    deadline = iso_date(row.get("limiteDepot"))
    written = iso_date(row.get("dateExamEcrit"))
    oral = iso_date(row.get("dateExamOral"))
    ees_name = (row.get("nomEes") or "").strip() or None

    review = []
    if not ees_name:
        review.append("Établissement à identifier")
    if not opening:
        review.append("Date d’ouverture absente ou incomplète dans la source")
    if not deadline:
        review.append("Date limite absente ou incomplète dans la source")
    if not (row.get("diplomeRequis") or "").strip():
        review.append("Diplôme requis à vérifier")

    return {
        "id": None,
        "nomConcours": (row.get("nomConcours") or "").strip(),
        "etat": None,
        "concoursBio": row.get("concoursBio"),
        "dateOuverture": opening,
        "limiteDepot": deadline,
        "dateExamEcrit": written,
        "dateExamOral": oral,
        "diplomeRequis": (row.get("diplomeRequis") or "").strip() or None,
        "adressExam": row.get("adressExam"),
        "eesId": None,
        "programmeId": None,
        "eesNameSource": ees_name,
        "programmeNameSource": row.get("programmeNom"),
        "sourceUrl": url,
        "sourceEvidence": evidence,
        "adminReviewNeeded": review,
    }


async def collect():
    items = []
    source_urls = []
    notes = []

    async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as html_crawler:
        articles, pdf_urls = await discover_documents(html_crawler)

        # Crawl des pages d'annonce et repérage de leurs PDF joints.
        for article_url in articles[:MAX_DOCUMENTS]:
            config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                page_timeout=60000,
                check_robots_txt=True,
            )
            page = await html_crawler.arun(url=article_url, config=config)
            if not page.success:
                notes.append(f"Article non récupéré : {article_url}")
                continue

            source_urls.append(article_url)
            soup = BeautifulSoup(page.html or "", "html.parser")
            for anchor in soup.select("a[href]"):
                target = urljoin(article_url, anchor.get("href", "")).split("#", 1)[0]
                if is_minesup(target) and is_pdf(target):
                    pdf_urls.append(target)

            data, text = await extract_from_url(html_crawler, article_url)
            for row in data.get("sessions", []):
                evidence = (row.get("preuve") or "").strip()
                if row.get("nomConcours") and evidence_found(evidence, text):
                    items.append(make_record(row, article_url, evidence))

    pdf_urls = list(dict.fromkeys(pdf_urls))[:MAX_DOCUMENTS]

    if pdf_urls:
        async with AsyncWebCrawler(
            crawler_strategy=PDFCrawlerStrategy()
        ) as pdf_crawler:
            for pdf_url in pdf_urls:
                data, text = await extract_from_url(pdf_crawler, pdf_url, is_pdf=True)
                if not data:
                    notes.append(f"PDF non extrait : {pdf_url}")
                    continue

                source_urls.append(pdf_url)
                for row in data.get("sessions", []):
                    evidence = (row.get("preuve") or "").strip()
                    if row.get("nomConcours") and evidence_found(evidence, text):
                        items.append(make_record(row, pdf_url, evidence))

    unique = {}
    for item in items:
        key = (
            norm(item["nomConcours"]),
            norm(item["eesNameSource"]),
            item["sourceUrl"],
        )
        unique[key] = item

    return list(unique.values()), source_urls, notes


if __name__ == "__main__":
    items, sources, notes = asyncio.run(collect())

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "table": "sessionAdmission",
        "status": "pending_admin_review",
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "sourceUrls": sorted(set(sources or [ARCHIVE_URL])),
        "count": len(items),
        "items": items,
        "notes": notes + [
            "Les IDs, etat et relations Prisma restent à résoudre après validation admin."
        ],
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Fichier créé : {OUTPUT.resolve()}")