"""Check every monitored page using the same honest identity the alert bot will use.
TLS verification stays ON. Blocks and certificate failures are reported, never bypassed.
Usage: python -I verify_sources.py sources.json source_report.csv
"""
import csv, json, re, ssl, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urlparse

UA = "UniversityCircularWatcher/0.1 (personal job-alert bot; read-only; low frequency)"
CTX = ssl.create_default_context()
JOB = re.compile(r"recruit|vacanc|job|career|circular|lecturer|faculty search|faculty position|officer|deadline|apply|নিয়োগ|প্রভাষক|কর্মকর্তা|বিজ্ঞপ্তি", re.I)
SPAM = re.compile(r"casino|gamstop|slot gacor|betting|judi online", re.I)
NOT_FOUND = re.compile(r"\b404\b|page not found|not found", re.I)

class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.title=""; self.t=False; self.skip=0; self.text=[]; self.links=0; self.pdfs=0
    def handle_starttag(self, tag, attrs):
        if tag=="title": self.t=True
        if tag in ("script","style","noscript"): self.skip+=1
        if tag=="a":
            self.links+=1
            if ".pdf" in (dict(attrs).get("href") or "").lower(): self.pdfs+=1
    def handle_endtag(self, tag):
        if tag=="title": self.t=False
        if tag in ("script","style","noscript") and self.skip: self.skip-=1
    def handle_data(self, d):
        if self.t: self.title+=d
        elif not self.skip: self.text.append(d)

def check(url):
    row={"url":url,"state":"","http":"","final_url":"","title":"","text_chars":0,"job_terms":0,"links":0,"pdfs":0,"detail":""}
    try:
        req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"text/html,application/xhtml+xml,*/*"})
        with urllib.request.urlopen(req,timeout=25,context=CTX) as r:
            row["http"]=r.status; row["final_url"]=r.geturl()
            raw=r.read(3_000_000); html=raw.decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        row["http"]=e.code; row["state"]="blocked" if e.code in (401,403,429,503) else "not_found" if e.code in (404,410) else "server_error"; return row
    except Exception as e:
        m=str(getattr(e,"reason",e)); row["detail"]=m[:100]
        row["state"]="cert_error" if "CERTIFICATE" in m.upper() else "timeout" if "timed out" in m.lower() else "dns_error" if "getaddrinfo" in m.lower() else "conn_error"; return row
    p=Page()
    try: p.feed(html)
    except Exception: pass
    text=" ".join(" ".join(p.text).split())
    row.update(title=" ".join(p.title.split())[:80],text_chars=len(text),job_terms=len(JOB.findall(text)),links=p.links,pdfs=p.pdfs)
    if SPAM.search(p.title+" "+text[:20000]): row["state"]="spam_warning"
    elif NOT_FOUND.search(p.title): row["state"]="soft_404"
    elif len(text)<200: row["state"]="needs_browser"
    elif row["job_terms"]==0: row["state"]="no_job_content"
    else: row["state"]="ok"
    return row

def run(item):
    out=[]
    for u in item["urls"]:
        r=check(u); r["name"]=item["name"]; r["type"]=item.get("type","backup"); out.append(r); time.sleep(0.4)
    return out

if __name__=="__main__":
    data=json.load(open(sys.argv[1],encoding="utf-8"))
    items=data["universities"]+data["backup_sources"]
    with ThreadPoolExecutor(8) as ex: rows=[r for g in ex.map(run,items) for r in g]
    fields=["name","type","state","http","url","final_url","title","text_chars","job_terms","links","pdfs","detail"]
    with open(sys.argv[2],"w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    uni=[i["name"] for i in data["universities"]]
    okset={r["name"] for r in rows if r["state"]=="ok"}
    print(f"universities={len(uni)} with_working_page={len(okset & set(uni))} pages={len(rows)}")
    states={}
    for r in rows: states[r["state"]]=states.get(r["state"],0)+1
    print(states)
    print("--- universities with NO ok page ---")
    for n in uni:
        if n not in okset:
            print(n, [(r["state"],r["http"],r["url"]) for r in rows if r["name"]==n])
    print("--- non-ok pages at universities that have another ok page ---")
    for r in rows:
        if r["state"]!="ok" and r["name"] in okset: print(r["name"],r["state"],r["http"],r["url"])
    print("--- backups ---")
    for r in rows:
        if r["type"]=="backup": print(r["name"],r["state"],r["http"],r["url"],r["job_terms"])
