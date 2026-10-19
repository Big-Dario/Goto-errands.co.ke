"""Fills in the SEO tags that need your website's address. Safe to run again whenever the address changes.

Usage:   python3 set_site_url.py https://big-dario.github.io/your-repo
         python3 set_site_url.py https://goto-errands.co.ke

It edits index.html (between the "seo:start" and "seo:end" markers) and writes robots.txt and sitemap.xml
next to it. Use the exact address people type to open the site: no trailing slash, no index.html.
"""
import json, re, sys
from datetime import date
from pathlib import Path

if len(sys.argv) < 2 or not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", sys.argv[1]):
    sys.exit(__doc__)
url = sys.argv[1].rstrip("/")
page = Path(sys.argv[2] if len(sys.argv) > 2 else "index.html")
html = page.read_text(encoding="utf-8")

desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
ld = {
    "@context": "https://schema.org", "@type": "LocalBusiness", "name": "SwiftRun", "description": desc,
    "url": url + "/", "image": url + "/og-image.png", "logo": url + "/icon-512.png",
    "telephone": "+254751696230",
    "contactPoint": [{"@type": "ContactPoint", "telephone": "+254751696230", "contactType": "customer service"},
                     {"@type": "ContactPoint", "telephone": "+254712331664", "contactType": "customer service"}],
    "address": {"@type": "PostalAddress", "addressLocality": "Nairobi", "addressCountry": "KE"},
    "areaServed": {"@type": "City", "name": "Nairobi"}, "paymentAccepted": "M-Pesa, Cash",
}
block = f'''<!-- seo:start (written by set_site_url.py, do not edit by hand) -->
<link rel="canonical" href="{url}/"/>
<meta property="og:url" content="{url}/"/>
<meta property="og:image" content="{url}/og-image.png"/>
<meta name="twitter:image" content="{url}/og-image.png"/>
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")}</script>
<!-- seo:end -->'''
new, n = re.subn(r"<!-- seo:start.*?<!-- seo:end -->", lambda m: block, html, count=1, flags=re.S)
if n != 1:
    sys.exit('Could not find the "seo:start" / "seo:end" markers in ' + str(page))
page.write_text(new, encoding="utf-8")

(page.parent / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {url}/sitemap.xml\n", encoding="utf-8")
(page.parent / "sitemap.xml").write_text(
    '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    f"  <url><loc>{url}/</loc><lastmod>{date.today().isoformat()}</lastmod></url>\n</urlset>\n", encoding="utf-8")
print(f"Done. Site address set to {url}/\n  - updated {page}\n  - wrote robots.txt and sitemap.xml")
if url.count("/") > 2:
    print("Note: your site lives under a path, so search engines ignore robots.txt there (they only read it at the domain root).\n"
          "      That's fine: submit the sitemap in Google Search Console instead.")
