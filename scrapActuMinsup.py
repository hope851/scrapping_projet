import asyncio
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode, BrowserConfig
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

def generate_slug(text: str) -> str:
    """Génère un slug propre et valide à partir d'un titre."""
    text = text.lower()
    # Remplacement des accents
    text = re.sub(r'[éèêëàâäôöûüç]', lambda m: {'é':'e','è':'e','ê':'e','ë':'e','à':'a','â':'a','ä':'a','ô':'o','ö':'o','û':'u','ü':'u','ç':'c'}[m.group(0)], text)
    # Remplacement de tout ce qui n'est pas alphanumérique par un tiret
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return slug

async def main():
    # URL ciblée pour éviter les blocages de la racine
    start_url = "https://minesup.gov.cm"
    current_url = start_url
    
    toutes_les_actualites = []

    # Configuration du navigateur furtif (Visible pour contourner le pare-feu du MINESUP)
    browser_config = BrowserConfig(
        headless=False,  
        ignore_https_errors=True,  
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        extra_args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-sandbox"
        ]
    )
    
    # 🎯 CONFIGURATION DE L'EXTRACTION ALIGNÉE SUR PRISMA
    extraction_strategy = JsonCssExtractionStrategy(
        schema={
            "name": "Actualités MINESUP",
            "baseSelector": "article, .post, .blog-item",  
            "fields": [
                {"name": "title", "selector": "h2 a, .post-title a, h3 a, .entry-title a", "type": "text"},
                {"name": "sourceUrl", "selector": "h2 a, .post-title a, h3 a, .entry-title a", "type": "attribute", "attribute": "href"},
                {"name": "image", "selector": "img, .wp-post-image, .post-thumbnail img", "type": "attribute", "attribute": "src"},
                {"name": "excerpt", "selector": ".entry-summary, .post-content, p", "type": "text"}
            ]
        }
    )

    # Configuration de navigation corrigée (Évite le networkidle qui cause des plantages)
    config = CrawlerRunConfig(
        extraction_strategy=extraction_strategy,
        cache_mode=CacheMode.BYPASS,  
        wait_until="domcontentloaded",  # Recommandé pour les infrastructures réseau locales
        page_timeout=30000,        
    )
    
    MAX_PAGES = 1  # Modifiez cette valeur pour récupérer plus de pages
    page_count = 0  

    async with AsyncWebCrawler(config=browser_config) as crawler:
        while current_url and page_count < MAX_PAGES:
            page_count += 1
            print(f"🕵️‍♂️ Scraping et extraction de la page {page_count} : {current_url}")
            
            result = await crawler.arun(url=current_url, config=config)
            
            if not result.success:
                print(f"❌ Échec du crawl sur {current_url}")
                break

            if result.extracted_content:
                donnees_page = json.loads(result.extracted_content)
                print(f"✅ {len(donnees_page)} articles détectés.")
                
                for item in donnees_page:
                    titre = item.get("title")
                    if not titre or titre.strip() == "":
                        continue
                    
                    titre = titre.strip()
                    
                    # 🎯 ADAPTATION STRICTE AUX CHAMPS DE VOTRE MODÈLE NEWS PRISMA
                    news_object = {
                        "title": titre,
                        "image": urljoin(current_url, item.get("image")) if item.get("image") else None,
                        "excerpt": item.get("excerpt", "Pas de résumé disponible").strip()[:180] + "...",
                        "content": item.get("excerpt", "").strip(),  # Contenu extrait initialement
                        "category": "Diaporama",
                        "slug": generate_slug(titre),
                        "isUrgent": False,
                        "sourceName": "MINESUP",
                        "sourceUrl": urljoin(current_url, item.get("sourceUrl")) if item.get("sourceUrl") else current_url,
                        "eesId": None,
                        "tutelleId": None,
                        "programmeId": None
                    }
                    toutes_les_actualites.append(news_object)

            # Recherche du bouton de pagination pour l'itération suivante
            soup = BeautifulSoup(result.html, 'html.parser')
            next_button = soup.select_one("li.next.right a, a.next, .nav-links .next, .pagination a.next")
            
            if next_button and next_button.get("href"):
                current_url = urljoin(current_url, next_button.get("href"))
            else:
                current_url = None

    # 💾 ENREGISTREMENT DU FICHIER JSON CONFORME PRISMA
    nom_fichier = "actualites_minesup.json"
    with open(nom_fichier, "w", encoding="utf-8") as f:
        json.dump(toutes_les_actualites, f, ensure_ascii=False, indent=4)
        
    print(f"\n🚀 Scraping terminé ! {len(toutes_les_actualites)} objets News enregistrés dans '{nom_fichier}'.")

if __name__ == "__main__":
    asyncio.run(main())

# "https://uy1.uninet.cm/evenements-et-actualites/"