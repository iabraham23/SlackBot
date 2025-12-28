"""
for fetching documents and creating doc_info

Documents are from https://support.fairly.com/en/, this code can be changed to accept other forms of documents as well 
"""


import json
import logging
from pathlib import Path
from bs4 import BeautifulSoup
import re
import requests
from urllib.parse import urljoin


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#set headers for get requests 
headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}

#URLs to fetch html from 
HOMEOWNER_URL = "https://support.fairly.com/en/collections/11081680-homeowners"
ADVISORS_URL = "https://support.fairly.com/en/collections/11088512-advisors"
CARETAKERS_URL = "https://support.fairly.com/en/collections/10244140-caretakers"


def get_html(URL: str, out: Path):
    collection_html = requests.get(URL, headers=headers, timeout=30).text
    soup = BeautifulSoup(collection_html, "html.parser")

    #Extract all article links on the page
    article_urls = []
    for a in soup.select('a[href*="/en/articles/"]'):
        href = a.get("href", "")
        if href:
            article_urls.append(urljoin(URL, href))

    # keep only real article URLs
    article_urls = sorted(set(u for u in article_urls if re.search(r"/en/articles/\d+", u)))

    print(f"Found {len(article_urls)} article URLs:")
    for u in article_urls:
        print(" -", u)

    for url in article_urls:
        slug = url.rstrip("/").split("/")[-1]
        slug = '-'.join(slug.split("-")[1:]) #gets rid of the numbers in the beginning 
        out_path = out / f"{slug}.html"
        html = requests.get(url, headers=headers, timeout=30).text
        out_path.write_text(html, encoding="utf-8")
        print("Saved", out_path)


def create_document(filepath: str) -> str | None:
    filepath = Path(filepath)
    if filepath.suffix == '.html':
        #Parsing the ArticleContent block from the html 
        try:
            with open(filepath, 'r', encoding='utf-8', errors="replace") as f:
                html = f.read()
            soup = BeautifulSoup(html, 'html.parser')
            title = str(soup.find('meta',{"property":"og:title"})["content"])
            short_description = str(soup.find('meta',{"property":"og:description"})["content"])
            script = soup.find('script', {"id": "__NEXT_DATA__"})
            data = json.loads(script.string)
            article_content = data["props"]["pageProps"]["articleContent"]
            blocktext = []
            pre = len(article_content["blocks"])
            print(f"length before: {pre}")
            for block in article_content["blocks"]:
                keys = set(block.keys())
                #print(keys)
                if "text" not in keys:
                    continue 
                blocktext.append(block["text"])
            print(f"length after: {len(blocktext)}")
            content_string = ''.join(blocktext)
            parent = filepath.parent.name
            doc = {"title":title, 
                   "short_description":short_description, 
                   "article":content_string, 
                   "category":parent} 

            out_path = f"doc_info/{doc['title'].replace(' ','_')}.json"
            with open(out_path, 'w') as f:
                json.dump(doc, f, indent=2)
            
            print(f"Done creating new document at {out_path}")
            return out_path

        except Exception as e:
            logger.error(f"Error reading {filepath}: {e}")
    else:
        print(f"{filepath} is not a valid file, must be an html")
        return 


def get_context(filepath:str):
    with open(filepath, 'r') as f:
        data = json.load(f)
    return data["article"]

def create_documents_from_parent(parent_path: str):
    parent_path = Path(parent_path)
    for path in parent_path.rglob('*'):
        if path.is_file():
            create_document(str(path))

if __name__ == "__main__":
    # OUT_DIR = Path("documents/Homeowners") #change to which folder 
    # OUT_DIR.mkdir(parents=True, exist_ok=True)
    # get_html(HOMEOWNER_URL, OUT_DIR)

    create_documents_from_parent("documents")
