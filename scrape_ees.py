import asyncio
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
)


WIKIPEDIA_URL = (
    "https://fr.wikipedia.org/wiki/"
    "Enseignement_sup%C3%A9rieur_au_Cameroun"
)
MINESUP_REGION_SEED = "https://www.minesup.gov.cm/index.php/centre/"
OUTPUT_FILE = Path("ees_programmes.json")

REQUEST_DELAY_SECONDS = 0.35
MIN_PROFILE_MATCH_SCORE = 84

CAMEROON_CITIES = [
    "Yaoundé", "Douala", "Buéa", "Buea", "Dschang", "Ngaoundéré",
    "Maroua", "Bamenda", "Bertoua", "Ebolowa", "Garoua", "Soa",
    "Bangangté", "Bafoussam", "Bafang", "Nanga-Eboko", "Sangmélima",
    "Obala", "Eséka", "Limbe", "Kumba", "Edéa", "Kribi", "Foumban",
    "Nkongsamba", "Mbalmayo", "Bambili", "Mfou", "Mbandjock",
]


def get_wikipedia_article_url(li, ees_name: str) -> str | None:
    """Trouve le lien Wikipédia qui correspond au nom de l'établissement."""
    best_url = None
    best_score = 0

    for anchor in li.select('a[href^="/wiki/"]'):
        title = clean_text(anchor.get_text(" ", strip=True))
        href = anchor.get("href", "")

        # Ignore les pages Wikipédia de catégorie, fichier, etc.
        page_name = href.split("/wiki/", 1)[-1].split("#", 1)[0]
        if ":" in page_name:
            continue

        score = fuzz.WRatio(ees_name, title)
        if score > best_score:
            best_score = score
            best_url = urljoin("https://fr.wikipedia.org", href.split("#")[0])

    return best_url if best_score >= 72 else None


def extract_wikipedia_bio(html: str) -> str | None:
    """Récupère jusqu'à deux paragraphes d'introduction de l'article."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#mw-content-text .mw-parser-output")

    if not content:
        return None

    paragraphs = []

    # On s'arrête au premier titre de section : on ne prend que l'introduction.
    for element in content.find_all(["p", "h2"], recursive=True):
        if element.name == "h2":
            break

        clone = BeautifulSoup(str(element), "html.parser")
        for node in clone.select("sup, .reference, .mw-editsection"):
            node.decompose()

        text = clean_text(clone.get_text(" ", strip=True))
        text = re.sub(r"\[\d+\]", "", text).strip()

        if len(text) >= 40:
            paragraphs.append(text)

        if len(paragraphs) == 2:
            break

    bio = clean_text(" ".join(paragraphs))
    return bio[:1200] if bio else None


def clean_text(value: str) -> str:
    """Nettoie les espaces et caractères de présentation."""
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize(value: str) -> str:
    """Normalise les accents et la casse pour comparer des noms."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return clean_text(value)


def canonical_url(url: str) -> str:
    """Retire le fragment et les paramètres inutiles d'une URL."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def host_is_minesup(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host.endswith("minesup.gov.cm")


def city_candidate(text: str | None) -> str | None:
    if not text:
        return None

    normalized_text = normalize(text)
    for city in CAMEROON_CITIES:
        if normalize(city) in normalized_text:
            return city

    return None


async def crawl_page(crawler, url: str):
    """Récupère une page avec Crawl4AI et applique une courte pause."""
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=1,
        page_timeout=60000,
        wait_until="domcontentloaded",
    )

    try:
        result = await crawler.arun(url=url, config=config)
        if not result.success:
            print(f"[AVERTISSEMENT] Échec du crawl {url}: {result.error_message}")
            return None, url

        await asyncio.sleep(REQUEST_DELAY_SECONDS)
        return result.html or result.cleaned_html or "", result.url or url

    except Exception as error:
        print(f"[AVERTISSEMENT] Impossible de crawler {url}: {error}")
        return None, url


def clean_list_item(li) -> str:
    """Extrait un item de liste Wikipédia sans les notes de bas de page."""
    clone = BeautifulSoup(str(li), "html.parser")

    for element in clone.select(
        "sup, .reference, .mw-editsection, ul, ol, style, script"
    ):
        element.decompose()

    return clean_text(clone.get_text(" ", strip=True))


def split_wikipedia_name_and_city(text: str) -> tuple[str, str | None]:
    """Sépare le nom de l'EES d'une ville lorsqu'elle est explicitement indiquée."""
    text = clean_text(text)
    found_city = city_candidate(text)

    if not found_city:
        return text, None

    patterns = [
        rf"\s*\(\s*{re.escape(found_city)}\s*\)\s*$",
        rf"\s*,\s*{re.escape(found_city)}\s*$",
    ]

    name = text
    for pattern in patterns:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE).strip()

    return name or text, found_city

def get_label_value(lines: list[str], label: str) -> str | None:
    """Trouve la valeur associée à un libellé MINESUP."""
    pattern = re.compile(
        rf"^\s*{label}\s*:\s*(.*)$",
        re.IGNORECASE,
    )

    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue

        value = clean_text(match.group(1))
        if value:
            return value

        # Parfois, la valeur apparaît sur la ligne suivante.
        if index + 1 < len(lines):
            next_line = clean_text(lines[index + 1])
            if next_line and ":" not in next_line:
                return next_line

    return None

def extract_wikipedia_ees(html: str) -> list[dict]:
    """Extrait les EES et le lien Wikipédia de leur article individuel."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#mw-content-text .mw-parser-output")

    if not content:
        raise RuntimeError("La zone principale de Wikipédia est introuvable.")

    in_target_section = False
    current_category = ""
    current_status = ""
    records = []
    seen = set()

    for element in content.find_all(
        ["h2", "h3", "h4", "ul", "ol"], recursive=True
    ):
        if element.name == "h2":
            heading = clean_text(element.get_text(" ", strip=True))

            if not in_target_section:
                if "etablissements d enseignement superieur" in normalize(heading):
                    in_target_section = True
                continue

            break

        if not in_target_section:
            continue

        if element.name == "h3":
            current_category = normalize(element.get_text(" ", strip=True))
            current_status = ""
            continue

        if element.name == "h4":
            current_status = normalize(element.get_text(" ", strip=True))
            continue

        if element.find_parent("li"):
            continue

        is_university_or_school = (
            "universite" in current_category
            or "ecole et institut" in current_category
            or "ecoles et instituts" in current_category
        )
        is_public = "public" in current_status
        is_private = "prive" in current_status

        if not is_university_or_school or not (is_public or is_private):
            continue

        for li in element.find_all("li", recursive=False):
            evidence = clean_list_item(li)
            if not evidence:
                continue

            name, wiki_city = split_wikipedia_name_and_city(evidence)
            if len(name) < 4:
                continue

            # Nouveau : mémorise la page Wikipédia propre à cet EES.
            article_url = get_wikipedia_article_url(li, name)

            ees_type = "PUBLIC" if is_public else "PRIVE"
            key = (normalize(name), ees_type)

            if key in seen:
                continue
            seen.add(key)

            records.append(
                {
                    "wikiName": name,
                    "eesType": ees_type,
                    "category": clean_text(current_category),
                    "wikiCity": wiki_city,
                    "wikipediaEvidence": evidence,

                    # Nouveau champ utilisé pour récupérer ees_bio.
                    "wikipediaArticleUrl": article_url,
                }
            )

    return records

def extract_programmes_from_profile(html: str) -> list[dict]:
    """
    Extrait les diplômes, filières et spécialités des fiches MINESUP.
    Un programme correspond à une filière, avec le diplôme comme niveau.
    """
    soup = BeautifulSoup(html, "html.parser")
    content = (
        soup.select_one(".entry-content")
        or soup.select_one("article")
        or soup.select_one("main")
        or soup.body
        or soup
    )

    text = clean_text(content.get_text(" ", strip=True))

    # Réduit les risques de lire les éléments de navigation ou de pied de page.
    start_match = re.search(r"fili[eè]res?", text, re.IGNORECASE)
    if start_match:
        text = text[start_match.start():]

    end_match = re.search(
        r"\b(?:Suivre\s*:|Nous contacter|Tous droits réservés)\b",
        text,
        re.IGNORECASE,
    )
    if end_match:
        text = text[:end_match.start()]

    label_pattern = re.compile(
        r"\b("
        r"Dipl[oô]me"
        r"|Fili[eè]re(?:\s+\d+)?"
        r"|Sp[eé]cialit[eé]s?"
        r")\s*:",
        re.IGNORECASE,
    )
    matches = list(label_pattern.finditer(text))

    current_level = None
    current_programme = None
    programmes = []

    def clean_value(value: str) -> str:
        value = clean_text(value.strip(" :-–—•·"))
        return value.rstrip(" .;")

    for index, match in enumerate(matches):
        next_start = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )
        value = clean_value(text[match.end():next_start])
        label = normalize(match.group(1))

        if not value:
            continue

        if label.startswith("diplome"):
            current_level = value
            current_programme = None

        elif label.startswith("filiere"):
            current_programme = {
                "nomProgramme": value,
                "specialite": None,
                "niveau": current_level,
                "sourceEvidence": value,
            }
            programmes.append(current_programme)

        elif label.startswith("specialite") and current_programme:
            current_programme["specialite"] = value
            current_programme["sourceEvidence"] += f" — Spécialités : {value}"

    # Élimine les doublons identiques issus de variantes de mise en page.
    unique = []
    seen = set()

    for programme in programmes:
        key = (
            normalize(programme["nomProgramme"]),
            normalize(programme.get("specialite") or ""),
            normalize(programme.get("niveau") or ""),
        )
        if key not in seen:
            seen.add(key)
            unique.append(programme)

    return unique


def extract_minesup_profile_data(html: str) -> dict:
    """Extrait les informations de la fiche institutionnelle MINESUP."""
    soup = BeautifulSoup(html, "html.parser")
    content = (
        soup.select_one(".entry-content")
        or soup.select_one("article")
        or soup.select_one("main")
        or soup.body
        or soup
    )

    lines = [
        clean_text(line)
        for line in content.get_text("\n", strip=True).splitlines()
        if clean_text(line)
    ]

    institution_name = get_label_value(
        lines,
        r"Nom de l['’]institution",
    )
    location = get_label_value(lines, r"Site de localisation")
    arrondissement = get_label_value(lines, r"Arrondissement")
    official_site = get_label_value(lines, r"Site web")

    if official_site:
        official_site = official_site.strip()
        if official_site and not official_site.startswith(("http://", "https://")):
            official_site = "https://" + official_site.lstrip("/")

    programmes = extract_programmes_from_profile(html)

    return {
        "institutionName": institution_name,
        "location": location,
        "arrondissement": arrondissement,
        "officialSite": official_site,
        "programmes": programmes,
    }

def find_region_urls(html: str, page_url: str) -> list[str]:
    """Repère les liens MINESUP vers les pages des régions."""
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    for anchor in soup.select("a[href]"):
        label = clean_text(anchor.get_text(" ", strip=True))
        url = canonical_url(urljoin(page_url, anchor["href"]))

        # normalize() retire les accents : "Région" devient "region".
        if "region" not in normalize(label):
            continue

        if host_is_minesup(url):
            urls.add(url)

    if not urls:
        urls.add(MINESUP_REGION_SEED)

    return sorted(urls)

def extract_minesup_profile_links(
    html: str,
    page_url: str,
    region_urls: set[str],
) -> list[dict]:
    """Récupère les liens des fiches d'établissements MINESUP."""
    soup = BeautifulSoup(html, "html.parser")
    content = (
        soup.select_one(".entry-content")
        or soup.select_one("article")
        or soup.select_one("main")
        or soup
    )

    profiles = {}

    for li in content.select("li"):
        for anchor in li.select("a[href]"):
            label = clean_text(anchor.get_text(" ", strip=True))
            url = canonical_url(urljoin(page_url, anchor["href"]))

            if not label or not host_is_minesup(url):
                continue
            if url in region_urls:
                continue

            path = urlsplit(url).path.lower()
            if any(part in path for part in ("/category/", "/tag/", "/page/")):
                continue
            if "region" in normalize(label):
                continue

            profiles[url] = {
                "directoryName": label,
                "url": url,
            }
            break

    return list(profiles.values())

def best_profile_for_ees(
    ees_name: str,
    profile_links: list[dict],
) -> tuple[dict | None, int, bool]:
    """Associe un EES à une fiche MINESUP si les noms se ressemblent."""
    ranked = []

    for profile in profile_links:
        score = fuzz.WRatio(ees_name, profile["directoryName"])
        ranked.append((score, profile))

    ranked.sort(key=lambda item: item[0], reverse=True)

    if not ranked or ranked[0][0] < MIN_PROFILE_MATCH_SCORE:
        return None, ranked[0][0] if ranked else 0, False

    ambiguous = (
        len(ranked) > 1
        and ranked[0][0] - ranked[1][0] < 6
        and normalize(ranked[0][1]["directoryName"])
        != normalize(ranked[1][1]["directoryName"])
    )

    if ambiguous:
        return None, ranked[0][0], True

    return ranked[0][1], ranked[0][0], False
async def main():
    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        # 1. Wikipédia : noms, statut public/privé et ville si mentionnée.
        wikipedia_html, wikipedia_actual_url = await crawl_page(
            crawler,
            WIKIPEDIA_URL,
        )
        if not wikipedia_html:
            raise RuntimeError("Impossible de lire la page Wikipédia.")

        wikipedia_ees = extract_wikipedia_ees(wikipedia_html)
        wikipedia_bios = {}
        bio_cache = {}

        for ees in wikipedia_ees:
            article_url = ees.get("wikipediaArticleUrl")
            if not article_url:
                continue

            if article_url not in bio_cache:
                article_html, _ = await crawl_page(crawler, article_url)
                bio_cache[article_url] = (
                    extract_wikipedia_bio(article_html)
                    if article_html
                    else None
                )

            wikipedia_bios[ees["wikiName"]] = bio_cache[article_url]
        print(f"{len(wikipedia_ees)} EES extraits de Wikipédia.")

        # 2. MINESUP : découvre les pages régionales depuis la page d'amorçage.
        seed_html, seed_actual_url = await crawl_page(
            crawler,
            MINESUP_REGION_SEED,
        )
        if not seed_html:
            raise RuntimeError("Impossible de lire la page régionale MINESUP.")

        region_urls = set(find_region_urls(seed_html, seed_actual_url))
        all_region_pages = {seed_actual_url: seed_html}

        for region_url in sorted(region_urls):
            if canonical_url(region_url) == canonical_url(seed_actual_url):
                continue

            html, actual_url = await crawl_page(crawler, region_url)
            if html:
                all_region_pages[actual_url] = html

        all_region_urls = {
            canonical_url(url) for url in all_region_pages.keys()
        }

        # 3. Collecte les fiches MINESUP listées dans les pages régionales.
        profile_links = {}
        for region_url, html in all_region_pages.items():
            for profile in extract_minesup_profile_links(
                html,
                region_url,
                all_region_urls,
            ):
                profile_links[profile["url"]] = profile

        profiles = list(profile_links.values())
        print(f"{len(profiles)} liens de fiches MINESUP trouvés.")

        # 4. Associe les profils aux noms Wikipédia par correspondance prudente.
        profile_by_wiki_name = {}
        ambiguous_names = set()
        selected_urls = set()

        for ees in wikipedia_ees:
            profile, score, ambiguous = best_profile_for_ees(
                ees["wikiName"],
                profiles,
            )

            if ambiguous:
                ambiguous_names.add(ees["wikiName"])
                continue

            if profile and profile["url"] not in selected_urls:
                selected_urls.add(profile["url"])
                profile_by_wiki_name[ees["wikiName"]] = {
                    **profile,
                    "matchScore": score,
                }

        # 5. Crawler les profils retenus pour obtenir site, localisation et programmes.
        profile_data_by_url = {}

        for profile in profile_by_wiki_name.values():
            html, _ = await crawl_page(crawler, profile["url"])
            if html:
                profile_data_by_url[profile["url"]] = (
                    extract_minesup_profile_data(html)
                )

    # 6. Prépare le JSON à transmettre à la plateforme de validation.
    records = []

    for ees in wikipedia_ees:
        profile_match = profile_by_wiki_name.get(ees["wikiName"])
        profile_data = (
            profile_data_by_url.get(profile_match["url"], {})
            if profile_match
            else {}
        )

        final_name = (
            profile_data.get("institutionName")
            or ees["wikiName"]
        )

        programmes = []
        for programme in profile_data.get("programmes", []):
            programme_copy = dict(programme)
            programme_copy["sourceUrl"] = profile_match["url"]
            programmes.append(programme_copy)

        city = (
            city_candidate(profile_data.get("arrondissement"))
            or city_candidate(profile_data.get("location"))
            or ees.get("wikiCity")
        )

        warnings = []
        missing_required_fields = []

        if not profile_match:
            warnings.append(
                "Aucune fiche MINESUP n'a été associée automatiquement."
            )
        if ees["wikiName"] in ambiguous_names:
            warnings.append(
                "Plusieurs fiches MINESUP ressemblent à cet EES : "
                "association à vérifier."
            )
        if not programmes:
            warnings.append(
                "Aucun programme n'a été extrait d'une fiche MINESUP."
            )

        # Ces valeurs sont nécessaires au formulaire ou à la relation Prisma.
        if not profile_data.get("institutionName") and not final_name:
            missing_required_fields.append("nom_ees")
        missing_required_fields.append("domaine")
        missing_required_fields.append("tutelleId")
        missing_required_fields.append("villeId")

        for index, programme in enumerate(programmes):
            if not programme.get("nomProgramme"):
                missing_required_fields.append(
                    f"programmes[{index}].nomProgramme"
                )
            if not programme.get("niveau"):
                missing_required_fields.append(
                    f"programmes[{index}].niveau"
                )

        if ees["eesType"] == "PUBLIC":
            tutelle_candidate = "MINESUP"
        else:
            tutelle_candidate = None

        records.append(
            {
                "reviewStatus": "PENDING_ADMIN_REVIEW",
                "formData": {
                    "nom_ees": final_name,
                    "domaine": None,
                    "ees_type": ees["eesType"],
                    "site_ees": profile_data.get("officialSite"),
                    "ees_bio": wikipedia_bios.get(ees["wikiName"]),
                    "programmes": programmes,
                },
                "relationsToResolve": {
                    "tutelleNameCandidate": tutelle_candidate,
                    "villeNameCandidate": city,
                    "paysNameCandidate": "Cameroun",
                    "tutelleId": None,
                    "villeId": None,
                },
                "source": {
                    "wikipedia": {
                        "url": wikipedia_actual_url,
                        "nameOnWikipedia": ees["wikiName"],
                        "category": ees["category"],
                        "evidence": ees["wikipediaEvidence"],
                        "bioUrl": ees.get("wikipediaArticleUrl"),
                        "bioLicense": "CC BY-SA 4.0",
                    },
                    "minsup": (
                        {
                            "url": profile_match["url"],
                            "directoryName": profile_match["directoryName"],
                            "matchScore": profile_match["matchScore"],
                        }
                        if profile_match
                        else None
                    ),
                },
                "missingRequiredFields": missing_required_fields,
                "warnings": warnings,
            }
        )

    output = {
        "schemaVersion": "1.0",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "status": "PENDING_ADMIN_REVIEW",
        "description": (
            "Collecte EES et programmes. Aucune insertion en base de données."
        ),
        "records": records,
    }

    OUTPUT_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Fichier créé : {OUTPUT_FILE.resolve()}")
    print(f"Nombre d'EES exportés : {len(records)}")


if __name__ == "__main__":
    asyncio.run(main())
    