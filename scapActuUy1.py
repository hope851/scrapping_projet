import asyncio
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode, BrowserConfig

def generate_slug(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[éèêëàâäôöûüç]', lambda m: {'é':'e','è':'e','ê':'e','ë':'e','à':'a','â':'a','ä':'a','ô':'o','ö':'o','û':'u','ü':'u','ç':'c'}[m.group(0)], text)
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return slug

async def main():
    liste_page_url = "https://uy1.uninet.cm/evenements-et-actualites/"
    
    # Configuration ultra-rapide (sans chargement d'images sur la page principale pour éviter le timeout)
    browser_config = BrowserConfig(
        headless=False,
        ignore_https_errors=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        headers={"Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"},
        extra_args=[
            "--disable-blink-features=AutomationControlled",  # 💡 Cache le pavillon "Robot/Automation"
            "--disable-infobars",
            "--no-sandbox"
        ] # Accélère la découverte des liens
    )

    # Configuration pour les articles individuels (on autorise le réseau léger)
    config_articles = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",  # Attend que la structure HTML complète (y compris le <body>) soit là
        page_timeout=60000,             # Augmenté à 60 secondes pour absorber les lenteurs du serveur local
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        print(f"🕵️‍♂️ Analyse de la page principale : {liste_page_url}")
        result_principal = await crawler.arun(url=liste_page_url, config=config_articles)
        
        if not result_principal.success:
            print(f"❌ Échec de la page principale : {result_principal.error_message}")
            return

        soup_main = BeautifulSoup(result_principal.html, 'html.parser')
        liens_bruts = soup_main.select("h3 a, .entry-title a, .post-title a") 
        
        urls_a_scraper = []
        for lien in liens_bruts:
            href = lien.get("href")
            if href and "category" not in href and href != "#":
                urls_a_scraper.append(urljoin(liste_page_url, href))
        
        urls_a_scraper = list(set(urls_a_scraper))
        print(f"🔗 {len(urls_a_scraper)} liens uniques trouvés !")

        # Limite de concurrence discrète pour éviter le CONNECTION_RESET
        semaphore = asyncio.Semaphore(2)
        liste_actualites = []

        async def scraper_un_article(url):
            async with semaphore:
                print(f"🕵️‍♂️ Extraction de : {url}")
                res = await crawler.arun(url=url, config=config_articles)
                await asyncio.sleep(1)
                return res

        print("\n🚀 Lancement du scraping contrôlé...")
        tasks = [scraper_un_article(url) for url in urls_a_scraper]
        resultats_articles = await asyncio.gather(*tasks)
        
        for res in resultats_articles:
            if res and res.success:
                soup_article = BeautifulSoup(res.html, 'html.parser')
                
                # 🎯 EXTRACTION DE L'IMAGE DE COUVERTURE VIA SÉLECTEURS WORDPRESS MULTIPLES
                image_url = None
                
                # 1. On cherche d'abord dans l'image mise en avant classique de WordPress
                img_tag = soup_article.select_one(".post-thumbnail img, .wp-post-image, article img, .entry-content img")
                if img_tag:
                    image_url = img_tag.get("src") or img_tag.get("data-src")
                
                # 2. En alternative, on regarde les métadonnées OpenGraph (og:image) récupérées par Crawl4AI
                if not image_url and res.metadata and res.metadata.get("og:image"):
                    image_url = res.metadata.get("og:image")
                
                # Normalisation du lien de l'image
                if image_url:
                    image_url = urljoin(res.url, image_url)

                titre = res.metadata.get("title") if res.metadata else "Titre non trouvé"
                if " - Université" in titre:
                    titre = titre.split(" - Université")[0].strip()
                
                article_data = {
                    "title": titre,
                    "image": image_url,  # 📷 Contient maintenant l'URL absolue de l'image trouvé
                    "excerpt": res.markdown[:180] + "..." if res.markdown else "",
                    "content": res.markdown if res.markdown else "",
                    "category": "Actualités Académiques",
                    "slug": generate_slug(titre),
                    "isUrgent": False,
                    "sourceName": "Université de Yaoundé 1",
                    "sourceUrl": res.url,
                    "eesId": None,
                    "tutelleId": None,
                    "programmeId": None
                }
                liste_actualites.append(article_data)
                print(f"✅ Réussi (Image: {image_url is not None}) : {res.url}")
            elif res:
                print(f"❌ Échec pour {res.url}")

        # Enregistrement JSON
        nom_fichier = "actualites_uy1.json"
        with open(nom_fichier, "w", encoding="utf-8") as f:
            json.dump(liste_actualites, f, ensure_ascii=False, indent=4)
            
        print(f"\n💾 Sauvegarde terminée ! {len(liste_actualites)} articles inclus avec leurs URLs d'images.")

if __name__ == "__main__":
    asyncio.run(main())
