MOROCCAN_MEDIA_FEEDS = [
    {"slug": "challenge",        "url": "https://www.challenge.ma/feed/",               "source_name": "Challenge.ma",            "target_table": "market", "also_risks": True, "language": "fr", "enabled": True},
    {"slug": "lavieeco",         "url": "https://lavieeco.com/feed/",                    "source_name": "La Vie éco",              "target_table": "market", "also_risks": True, "language": "fr", "enabled": True},
    {"slug": "industries_maroc", "url": "https://industries.ma/",                         "source_name": "Industrie du Maroc",      "target_table": "market", "also_risks": True, "language": "fr", "enabled": True, "needs_curl": True, "feed_type": "industries_scrape"},
    {"slug": "aujourdhui_maroc", "url": "https://aujourdhui.ma/feed",                    "source_name": "Aujourd'hui le Maroc",    "target_table": "market", "also_risks": True, "language": "fr", "enabled": True},
    {"slug": "hespress",         "url": "https://m.hespress.com/feed",                   "source_name": "Hespress",                "target_table": "market", "also_risks": True, "language": "fr", "enabled": True},
    {"slug": "medi1",            "url": "https://www.medi1.com/fr/rss",                  "source_name": "Medi1",                   "target_table": "market", "language": "fr",   "enabled": False},
    {"slug": "medias24",         "url": "https://medias24.com/feed/",                    "source_name": "Medias24",                "target_table": "market", "also_risks": True, "language": "fr", "enabled": True},
    {"slug": "medias24_eco",     "url": "https://medias24.com/categorie/economie/feed/", "source_name": "Medias24 Économie",       "target_table": "market", "also_risks": True, "language": "fr", "enabled": True},
]

MOROCCAN_REGULATORY_FEEDS = [
    {"slug": "bam_communiques",  "url": "https://www.bkam.ma/Communiques/feed",    "source_name": "Bank Al-Maghrib",         "target_table": "alert",  "alert_type": "compliance", "regulator": "BAM",  "language": "fr", "enabled": False},
]

INTERNATIONAL_MACRO_FEEDS = [
    {"slug": "imf_news",         "url": "https://www.imf.org/en/News",                                  "source_name": "IMF",             "target_table": "market", "language": "en", "enabled": True, "needs_curl": True, "feed_type": "imf_scrape"},
    {"slug": "ecb_press",        "url": "https://www.ecb.europa.eu/rss/press.html",                     "source_name": "ECB Press",       "target_table": "market", "language": "en", "enabled": True},
    {"slug": "ecb_blog",         "url": "https://www.ecb.europa.eu/rss/blog.html",                      "source_name": "ECB Blog",        "target_table": "market", "language": "en", "enabled": True},
    {"slug": "ecb_stats",        "url": "https://www.ecb.europa.eu/rss/statpress.html",                 "source_name": "ECB Stats",       "target_table": "market", "language": "en", "enabled": True},
    {"slug": "oecd",             "url": "https://api.oecd.org/webcms/search/rss?siteName=oecd&interfaceLanguage=en&path=%2Fcontent%2Foecd%2Fen%2Fabout%2Fnews&facets=oecd-content-types%3Anews%2Fpress-releases&facets=oecd-languages%3Aen", "source_name": "OECD Economy",    "target_table": "market", "language": "en", "enabled": True},
    {"slug": "eu_digital",       "url": "https://digital-strategy.ec.europa.eu/en/rss.xml",             "source_name": "EU Digital",      "target_table": "market", "language": "en", "enabled": True},
    {"slug": "afdb_news",        "url": "https://www.afdb.org/en/news-and-events/rss",                  "source_name": "AfDB",            "target_table": "market", "language": "en", "enabled": True, "needs_curl": True},
    {"slug": "un_news",          "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml",        "source_name": "UN News",          "target_table": "market", "language": "en", "enabled": True},
    {"slug": "eu_commission",    "url": "https://ec.europa.eu/commission/presscorner/api/rss",           "source_name": "European Commission", "target_table": "market", "language": "en", "enabled": True},
]

CYBERSECURITY_FEEDS = [
    {
        "slug": "cisa",
        "source_name": "CISA - Known Exploited Vulnerabilities",
        "url": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        "target_table": "alert",
        "language": "en",
        "alert_type": "cyber",
        "cyber_source": "CISA",
        "feed_type": "json",
        "max_items": 50,
        "enabled": True,
    },
    {
        "slug": "enisa",
        "source_name": "ENISA",
        "url": "https://www.enisa.europa.eu/sitemap.xml",
        "target_table": "alert",
        "language": "en",
        "alert_type": "cyber",
        "cyber_source": "ENISA",
        "feed_type": "sitemap",
        "sitemap_filter": "/news/",
        "enabled": True,
    },
]

# Feeds confirmed blocked — do not enable without testing the new URL first
BLOCKED_FEEDS = [
    {"slug": "oecd_rdf_old",  "url": "https://www.oecd.org/newsroom/index.rdf",         "enabled": False},  # 403 Cloudflare — replaced by API RSS
    {"slug": "enisa_old",     "url": "https://www.enisa.europa.eu/news/enisa-news/RSS", "enabled": False},  # 404 — replaced by sitemap
    {"slug": "lemonde",       "url": "https://www.lemonde.fr/actualite-maroc/rss_full.xml", "enabled": False},  # Low Morocco coverage
    {"slug": "medi1",         "url": "https://www.medi1.com/fr/rss",                    "enabled": False},  # Network timeout — site unreachable
    {"slug": "bam_feed_dead", "url": "https://www.bkam.ma/Communiques/feed",            "enabled": False},  # 404 — replaced by dynamic scraper
]

ALL_FEEDS = (
    MOROCCAN_MEDIA_FEEDS
    + MOROCCAN_REGULATORY_FEEDS
    + INTERNATIONAL_MACRO_FEEDS
    + CYBERSECURITY_FEEDS
    + BLOCKED_FEEDS
)

ACTIVE_FEEDS = [f for f in ALL_FEEDS if f.get("enabled", True)]