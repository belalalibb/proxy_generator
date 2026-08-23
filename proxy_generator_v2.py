# -*- coding: utf-8 -*-
"""
=============================================================
  Proxy Scraper & Filter v1.0 — Synottip Edition
  يجمع بروكسيات من 80+ مصدر ويفلترها على HTTPS + Synottip
  المصادر: v1.py + v2.py + v3.py + test_proxy.py APIs
=============================================================
"""
import sys, os, re, json, time, random, socket, threading, argparse
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from datetime import datetime
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    C = Fore.CYAN; G = Fore.GREEN; R = Fore.RED; Y = Fore.YELLOW
    M = Fore.MAGENTA; W = Fore.WHITE; B = Style.BRIGHT; RST = Style.RESET_ALL
except ImportError:
    C = G = R = Y = M = W = B = RST = ""

# ══════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════
# PyInstaller: لما يكون EXE، المسار يكون فولدر الـ EXE مش temp
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_URL = "https://www.media.io/"
FALLBACK_TEST = "https://www.media.io/"
MAX_WORKERS_COLLECT = 20
MAX_WORKERS_TEST = 100
TIMEOUT_COLLECT = 12
TIMEOUT_TEST = 10
MAX_PROXIES = 15000
RETRY_COUNT = 2

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36',
]

# ══════════════════════════════════════════════════════════════
# All Proxy Sources (merged from v1 + v2 + v3 + test_proxy APIs)
# ══════════════════════════════════════════════════════════════
RAW_SOURCES = [
    # GitHub Raw Files (v1 + v3 merged, deduplicated)
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/https.txt",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt",
    "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt",
    "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt",
    "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt",
    "https://raw.githubusercontent.com/manuGMG/proxy-365/main/HTTP.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies_anonymous/http.txt",
    "https://raw.githubusercontent.com/saschazesiger/Free-Proxies/master/proxies/http.txt",
    "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt",
    "https://raw.githubusercontent.com/runarbu/ProxyMaid/master/lists/proxies.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS.txt",
    # ── New Strong Sources ──
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt",
    "https://raw.githubusercontent.com/im-razvan/proxy_list/main/http.txt",
    "https://raw.githubusercontent.com/ObcbO/getproxy/master/http.txt",
    "https://raw.githubusercontent.com/ObcbO/getproxy/master/https.txt",
    "https://raw.githubusercontent.com/yemixzy/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/Bardiafa/Proxy-Starter/main/http.txt",
    "https://raw.githubusercontent.com/Bardiafa/Proxy-Starter/main/https.txt",
    "https://raw.githubusercontent.com/aslisk/proxyhttps/main/https.txt",
    "https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/free.txt",
    "https://raw.githubusercontent.com/Tsprnay/Proxy-lists/master/proxies/http.txt",
    "https://raw.githubusercontent.com/Tsprnay/Proxy-lists/master/proxies/https.txt",
    "https://raw.githubusercontent.com/yoannchb-pro/https-proxies/main/proxies.txt",
    "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/https.txt",
    "https://raw.githubusercontent.com/berkay-digital/Proxy-Starter/main/http.txt",
    # API Endpoints
    "https://api.openproxylist.xyz/http.txt",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=https",
    "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&proxy_format=protocolipport&format=text",
    "https://www.proxy-list.download/api/v1/get?type=http",
    "https://www.proxy-list.download/api/v1/get?type=https",
    "https://proxyspace.pro/http.txt",
    "https://proxyspace.pro/https.txt",
    "https://spys.me/proxy.txt",
    "https://multiproxy.org/txt_all/proxy.txt",
    # CDN Sources
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt",
    # Additional Sources
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt",
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/https_proxies.txt",
    "https://raw.githubusercontent.com/caliphdev/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/https.txt",
    "https://raw.githubusercontent.com/Hendrikbgr/Free-Proxy-Repo/master/proxy_list.txt",
    "https://raw.githubusercontent.com/Kitsun3Sec/ProxyList/main/http.txt",
    "https://raw.githubusercontent.com/Zeller-A/Proxy-Scraping/main/http.txt",
    "https://raw.githubusercontent.com/Zeller-A/Proxy-Scraping/main/https.txt",
    "https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list/data.txt",
    "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/tuanminpay/live-proxy/master/http.txt",
    "https://api.proxyscrape.com/?request=getproxies&proxytype=http&timeout=10000&country=all&ssl=yes&anonymity=all",
]

WEB_SOURCES = [
    "https://free-proxy-list.net/",
    "https://free-proxy-list.net/anonymous-proxy.html",
    "https://www.sslproxies.org/",
    "https://www.us-proxy.org/",
    "https://www.proxy-list.download/HTTPS",
    "https://www.proxy-list.download/HTTP",
    "https://advanced.name/freeproxy",
    "https://advanced.name/freeproxy?type=https",
    "https://geonode.com/free-proxy-list",
    "https://list.proxylistplus.com/SSL-proxy",
    "https://proxybros.com/free-proxy-list/",
    "https://www.freeproxy.world/",
    "https://www.proxynova.com/proxy-server-list/",
    "https://freeproxylist.org/",
]

# GeoNode JSON API (from v2)
GEONODE_API = "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=https"

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════
def rand_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
    }

def validate_proxy(proxy_str):
    """Validate ip:port format, reject private IPs"""
    try:
        if ':' not in proxy_str:
            return False
        ip, port = proxy_str.split(':', 1)
        ip, port = ip.strip(), port.strip()
        parts = [int(x) for x in ip.split('.')]
        if len(parts) != 4 or not all(0 <= p <= 255 for p in parts):
            return False
        if not (1 <= int(port) <= 65535):
            return False
        if ip.startswith(('127.', '192.168.', '10.', '172.16.', '169.254.')) or ip == '0.0.0.0':
            return False
        return True
    except (ValueError, AttributeError):
        return False

def extract_proxies(text):
    """Extract ip:port patterns from text"""
    if not text:
        return set()
    patterns = [
        r'\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b',
        r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s*:\s*(\d{1,5})',
        r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+(\d{1,5})',
    ]
    found = set()
    for pat in patterns:
        matches = re.findall(pat, text)
        if matches and isinstance(matches[0], tuple):
            for m in matches:
                found.add(f"{m[0]}:{m[1]}")
        else:
            found.update(matches)
    return {p for p in found if validate_proxy(p)}

# ══════════════════════════════════════════════════════════════
# Scraper Class
# ══════════════════════════════════════════════════════════════
class ProxyScraper:
    def __init__(self, target_url=TARGET_URL):
        self.target_url = target_url
        self.all_proxies = set()
        self.working_proxies = []
        self.tested = 0
        self.source_stats = {}
        self.t0 = time.time()

    # ── Collection ──
    def fetch_raw(self, url):
        """Fetch proxies from raw text URL"""
        name = urlparse(url).netloc[:30]
        try:
            r = requests.get(url, headers=rand_headers(), timeout=TIMEOUT_COLLECT,
                             verify=False, allow_redirects=True)
            if r.status_code == 200:
                proxies = extract_proxies(r.text)
                if proxies:
                    self.source_stats[name] = self.source_stats.get(name, 0) + len(proxies)
                return proxies
        except Exception:
            pass
        return set()

    def fetch_web(self, url):
        """Scrape web page for proxies (with HTML table parsing)"""
        name = urlparse(url).netloc[:30]
        try:
            r = requests.get(url, headers=rand_headers(), timeout=TIMEOUT_COLLECT, verify=False)
            if r.status_code != 200:
                return set()

            proxies = set()
            # Try HTML table parsing with BeautifulSoup
            if HAS_BS4 and ('free-proxy-list' in url or 'sslproxies' in url or
                            'us-proxy' in url or 'proxy-list.download' in url):
                soup = BeautifulSoup(r.text, 'html.parser')
                # textarea method
                ta = soup.find('textarea')
                if ta:
                    for line in ta.get_text().split('\n'):
                        line = line.strip()
                        if validate_proxy(line):
                            proxies.add(line)
                # table method
                if not proxies:
                    table = soup.find('table')
                    if table:
                        for row in table.find_all('tr')[1:]:
                            cols = row.find_all('td')
                            if len(cols) >= 2:
                                ip = cols[0].get_text(strip=True)
                                port = cols[1].get_text(strip=True)
                                p = f"{ip}:{port}"
                                if validate_proxy(p):
                                    proxies.add(p)

            # Fallback: regex extraction
            if not proxies:
                proxies = extract_proxies(r.text)

            if proxies:
                self.source_stats[name] = self.source_stats.get(name, 0) + len(proxies)
            return proxies
        except Exception:
            return set()

    def fetch_geonode(self):
        """GeoNode JSON API (from v2)"""
        try:
            r = requests.get(GEONODE_API, headers=rand_headers(), timeout=TIMEOUT_COLLECT)
            if r.status_code == 200:
                data = r.json()
                proxies = set()
                for item in data.get('data', []):
                    ip = item.get('ip')
                    port = item.get('port')
                    if ip and port:
                        p = f"{ip}:{port}"
                        if validate_proxy(p):
                            proxies.add(p)
                if proxies:
                    self.source_stats['geonode-api'] = len(proxies)
                return proxies
        except Exception:
            pass
        return set()

    def collect_all(self):
        """Collect from all sources in parallel"""
        print(f"\n{C}{B}{'='*60}")
        print(f"  Proxy Collector — {len(RAW_SOURCES)} raw + {len(WEB_SOURCES)} web sources")
        print(f"{'='*60}{RST}\n")

        # Raw sources
        print(f"  {Y}[1/3]{RST} Fetching from {len(RAW_SOURCES)} raw sources...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_COLLECT) as ex:
            futures = {ex.submit(self.fetch_raw, url): url for url in RAW_SOURCES}
            for f in as_completed(futures):
                try:
                    self.all_proxies.update(f.result())
                except Exception:
                    pass
        print(f"  {G}  → {len(self.all_proxies)} proxies so far{RST}")

        # Web sources
        print(f"  {Y}[2/3]{RST} Scraping {len(WEB_SOURCES)} web sources...")
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(self.fetch_web, url): url for url in WEB_SOURCES}
            for f in as_completed(futures):
                try:
                    self.all_proxies.update(f.result())
                except Exception:
                    pass
        print(f"  {G}  → {len(self.all_proxies)} proxies so far{RST}")

        # GeoNode API
        print(f"  {Y}[3/3]{RST} GeoNode API...")
        self.all_proxies.update(self.fetch_geonode())
        print(f"  {G}  → {len(self.all_proxies)} total unique proxies{RST}")

        # Also load existing proxy.txt if it has entries
        proxy_file = os.path.join(BASE_DIR, "proxy.txt")
        if os.path.exists(proxy_file):
            try:
                lines = open(proxy_file, 'r', encoding='utf-8').read().splitlines()
                existing = {l.strip().split('#')[0].strip() for l in lines
                            if l.strip() and not l.strip().startswith('#')}
                valid_existing = {p for p in existing if validate_proxy(p)}
                if valid_existing:
                    self.all_proxies.update(valid_existing)
                    print(f"  {M}  + {len(valid_existing)} from existing proxy.txt{RST}")
            except Exception:
                pass

        # Cap
        if len(self.all_proxies) > MAX_PROXIES:
            self.all_proxies = set(list(self.all_proxies)[:MAX_PROXIES])

        # Print top sources
        print(f"\n  {W}Top sources:{RST}")
        for src, cnt in sorted(self.source_stats.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"    {C}{src:<30}{RST} {cnt} proxies")

        return self.all_proxies

    # ── Testing ──
    def test_one(self, proxy):
        """Test proxy against target URL with retries"""
        for attempt in range(RETRY_COUNT):
            try:
                proxy_dict = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
                t0 = time.time()
                r = requests.get(
                    self.target_url,
                    proxies=proxy_dict,
                    headers=rand_headers(),
                    timeout=TIMEOUT_TEST,
                    verify=False,
                    allow_redirects=True,
                )
                ms = int((time.time() - t0) * 1000)

                if r.status_code == 200 and len(r.text) > 1000:
                    return {'proxy': proxy, 'ok': True, 'ms': ms, 'status': r.status_code}

                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.3)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.3)
            except Exception:
                break

        return {'proxy': proxy, 'ok': False}

    def test_all(self):
        """Test all collected proxies in parallel"""
        total = len(self.all_proxies)
        if total == 0:
            print(f"  {R}No proxies to test!{RST}")
            return

        print(f"\n{C}{B}{'='*60}")
        print(f"  Testing {total} proxies against: {self.target_url}")
        print(f"  Workers: {MAX_WORKERS_TEST} | Timeout: {TIMEOUT_TEST}s | Retries: {RETRY_COUNT}")
        print(f"{'='*60}{RST}\n")

        self.tested = 0
        working = 0
        t_start = time.time()

        # Load existing proxies to avoid duplicates (DON'T clear the file)
        self.proxy_file = os.path.join(BASE_DIR, "proxy.txt")
        self._file_lock = threading.Lock()
        self._existing = set()
        try:
            lines = open(self.proxy_file, 'r', encoding='utf-8').read().splitlines()
            self._existing = {l.strip() for l in lines if l.strip()}
        except FileNotFoundError:
            pass

        def progress():
            while self.tested < total:
                elapsed = time.time() - t_start
                rate = self.tested / elapsed if elapsed > 0 else 0
                pct = self.tested / total * 100
                eta = (total - self.tested) / rate if rate > 0 else 0
                print(f"\r  {W}Testing: {self.tested}/{total} ({pct:.0f}%) "
                      f"| {G}OK: {working}{RST} | {W}{rate:.0f}/s | ETA: {eta:.0f}s{RST}   ",
                      end='', flush=True)
                time.sleep(1)

        pt = threading.Thread(target=progress, daemon=True)
        pt.start()

        proxies_list = list(self.all_proxies)
        random.shuffle(proxies_list)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS_TEST) as ex:
            futs = {ex.submit(self.test_one, p): p for p in proxies_list}
            for f in as_completed(futs):
                try:
                    res = f.result()
                    self.tested += 1
                    if res.get('ok'):
                        self.working_proxies.append(res)
                        working += 1
                        # Save to proxy.txt immediately (skip duplicates)
                        with self._file_lock:
                            if res['proxy'] not in self._existing:
                                with open(self.proxy_file, 'a') as f:
                                    f.write(f"{res['proxy']}\n")
                                self._existing.add(res['proxy'])
                        print(f"\r  {G}{B}✅ {res['proxy']:<22} {res['ms']}ms  → saved{RST}" + " "*20)
                except Exception:
                    self.tested += 1

        print(f"\r" + " " * 70)
        dur = time.time() - t_start
        print(f"\n  {G}{B}Done!{RST} {len(self.working_proxies)} working / {total} tested in {dur:.0f}s")

    # ── Save ──
    def save(self, filename=None):
        """Save working proxies to proxy.txt (sorted by speed)"""
        if not filename:
            filename = os.path.join(BASE_DIR, "proxy.txt")

        sorted_proxies = sorted(self.working_proxies, key=lambda x: x.get('ms', 9999))

        with open(filename, 'w', encoding='utf-8') as f:
            for p in sorted_proxies:
                f.write(f"{p['proxy']}\n")

        print(f"\n  {G}{B}Saved {len(sorted_proxies)} proxies to {filename}{RST}")
        print(f"  {W}Sorted by speed (fastest first){RST}")

        if sorted_proxies:
            print(f"\n  {C}{B}Top 10 fastest:{RST}")
            for i, p in enumerate(sorted_proxies[:10], 1):
                print(f"    {i:2d}. {p['proxy']:<22} — {p['ms']}ms")

        return sorted_proxies

    # ── Run (single cycle) ──
    def run_once(self):
        """دورة واحدة: جمع + فحص + حفظ"""
        self.all_proxies = set()
        self.working_proxies = []
        self.tested = 0
        self.source_stats = {}

        self.collect_all()
        if not self.all_proxies:
            print(f"  {R}No proxies found!{RST}")
            return []
        self.test_all()
        return self.working_proxies

    # ── Run (infinite loop) ──
    def run_loop(self, pause=60):
        """لوب مفتوح: جمع → فحص → حفظ → انتظار → تكرار"""
        cycle = 0
        total_found = 0

        print(f"\n{C}{B}{'='*60}")
        print(f"  🚀 Proxy Scraper & Filter — Synottip Edition")
        print(f"  ♾️  Infinite Loop Mode — Ctrl+C to stop")
        print(f"{'='*60}{RST}")
        print(f"  {W}Target: {self.target_url}{RST}")
        print(f"  {W}Pause between cycles: {pause}s{RST}\n")

        try:
            while True:
                cycle += 1
                print(f"\n{Y}{B}{'─'*60}")
                print(f"  🔄 Cycle #{cycle} — Total found so far: {total_found}")
                print(f"{'─'*60}{RST}")

                found = self.run_once()
                total_found += len(found)

                if found:
                    print(f"\n  {G}{B}Cycle #{cycle} done: +{len(found)} working proxies (total: {total_found}){RST}")
                else:
                    print(f"\n  {Y}Cycle #{cycle} done: no new working proxies{RST}")

                # Show current proxy.txt count
                try:
                    lines = open(os.path.join(BASE_DIR, "proxy.txt"), 'r').read().splitlines()
                    active = len([l for l in lines if l.strip()])
                    print(f"  {M}📄 proxy.txt: {active} active proxies{RST}")
                except Exception:
                    pass

                print(f"\n  {W}⏳ Waiting {pause}s before next cycle... (Ctrl+C to stop){RST}")
                time.sleep(pause)

        except KeyboardInterrupt:
            print(f"\n\n  {R}{B}⛔ Stopped by Ctrl+C{RST}")
            print(f"  {G}Total proxies found across {cycle} cycles: {total_found}{RST}")

# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════
def main():
    global MAX_WORKERS_TEST, TIMEOUT_TEST
    parser = argparse.ArgumentParser(description="Proxy Scraper & Filter for Synottip")
    parser.add_argument("--target", default=TARGET_URL, help="Target URL to test proxies against")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS_TEST, help="Test threads")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_TEST, help="Test timeout (seconds)")
    parser.add_argument("--collect-only", action="store_true", help="Collect without testing")
    parser.add_argument("--pause", type=int, default=60, help="Pause between cycles in seconds (default: 60)")
    parser.add_argument("--output", default=None, help="Output file (default: proxy.txt)")
    args = parser.parse_args()

    MAX_WORKERS_TEST = args.workers
    TIMEOUT_TEST = args.timeout

    scraper = ProxyScraper(target_url=args.target)

    if args.collect_only:
        scraper.collect_all()
        out = args.output or os.path.join(BASE_DIR, "proxy_raw.txt")
        with open(out, 'w') as f:
            for p in sorted(scraper.all_proxies):
                f.write(f"{p}\n")
        print(f"\n  {G}Saved {len(scraper.all_proxies)} raw proxies to {out}{RST}")
    else:
        scraper.run_loop(pause=args.pause)

if __name__ == "__main__":
    main()
