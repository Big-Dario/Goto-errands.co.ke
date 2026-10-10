"""Wires the SwiftRun add-ons into index.html. Safe to run more than once.
Usage: python3 install.py index.html"""
import re, sys
path = sys.argv[1] if len(sys.argv) > 1 else "index.html"
h = open(path, encoding="utf-8").read()
done = []

def add_before(text, marker, snippet, last=True):
    global h
    if snippet in h or marker not in h:
        return
    i = h.rindex(marker) if last else h.index(marker)
    h = h[:i] + snippet + "\n" + h[i:]
    done.append(snippet)

pre = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
       '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
if "rel=\"preconnect\"" not in h and "<head>" in h:
    h = h.replace("<head>", "<head>\n" + pre, 1); done.append("font preconnect")

add_before(h, "</head>", '<link rel="stylesheet" href="responsive.css">')
add_before(h, "</body>", '<script src="categories.js"></script>')
add_before(h, "</body>", '<script src="performance.js"></script>')
add_before(h, "</body>", '<script src="config.js"></script>')
add_before(h, "</body>", '<script src="backend.js"></script>')

new = re.sub(r'<meta name="viewport"[^>]*>',
             '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content"/>',
             h, count=1)
if new != h and "viewport-fit" not in h:
    h = new; done.append("viewport meta")

open(path, "w", encoding="utf-8").write(h)
print("Added:" if done else "Nothing to add, already installed.")
for d in done: print(" -", d)
