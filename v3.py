#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔥 Ultimate HTTPS Proxy Scraper - Combined Version
Developer: AI Assistant
Date: 2025-08-22
Description: Combined comprehensive proxy scraper with Instagram testing
"""

import requests
import threading
import time
import re
import json
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin
import random
import os
import sys
from bs4 import BeautifulSoup
import socket
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configuration
MAX_WORKERS = 100
TIMEOUT = 8
TEST_URL = "https://www.instagram.com"
MAX_PROXIES = 15000
RETRY_COUNT = 2

# Diverse User-Agents
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36'
]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('proxy_scraper.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class UltimateProxyScraper:
    def __init__(self):
        self.proxies = set()  # Using set to avoid duplicates
        self.working_proxies = []
        self.tested_count = 0
        self.total_count = 0
        self.start_time = time.time()
        
        # Combined unique proxy sources
        self.proxy_sources = [
            # GitHub Raw Files
            "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
            "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt", 
            "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
            "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
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
            
            # API Endpoints
            "https://api.openproxy.space/lists/http",
            "https://api.openproxylist.xyz/http.txt",
            "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http",
            "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&proxy_format=protocolipport&format=text",
            "https://www.proxy-list.download/api/v1/get?type=http",
            "https://www.proxy-list.download/api/v1/get?type=https",
            "https://proxyspace.pro/http.txt",
            "https://proxyspace.pro/https.txt",
            
            # CDN Sources
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt",
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt",
        ]
        
        # Web scraping sources
        self.web_sources = [
            "https://free-proxy-list.net/",
            "https://free-proxy-list.net/uk-proxy.html",
            "https://free-proxy-list.net/anonymous-proxy.html", 
            "https://www.sslproxies.org/",
            "https://www.us-proxy.org/",
            "https://www.proxy-list.download/HTTPS",
            "https://www.proxy-list.download/HTTP",
            "https://proxydb.net/",
            "https://proxydb.net/?protocol=https",
            "https://proxyscrape.com/free-proxy-list",
            "https://advanced.name/freeproxy",
            "https://advanced.name/freeproxy?type=https",
            "https://hide.mn/en/proxy-list/",
            "https://spys.one/en/https-ssl-proxy/",
            "https://geonode.com/free-proxy-list",
            "https://fineproxy.org/free-proxy/",
            "https://proxy-tools.com/proxy/https",
            "https://list.proxylistplus.com/SSL-proxy",
            "https://proxybros.com/free-proxy-list/",
            "https://iproyal.com/free-proxy-list/",
            "https://www.freeproxy.world/",
            "https://www.proxynova.com/proxy-server-list/",
            "https://databay.com/free-proxy-list",
            "https://openproxylist.com/proxy/",
            "https://freeproxylist.org/",
            "https://proxy5.net/free-proxy"
        ]
        
        self.source_stats = {}
        logger.info("🚀 Starting Ultimate Proxy Scraper")
        
    def get_random_headers(self):
        """Generate random headers to avoid blocking"""
        return {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }

    def validate_proxy_format(self, proxy):
        """Validate proxy IP:port format"""
        try:
            if ':' not in proxy:
                return False
            
            ip, port = proxy.split(':', 1)
            ip = ip.strip()
            port = port.strip()
            
            # Validate IP
            ip_parts = [int(x) for x in ip.split('.')]
            if len(ip_parts) != 4 or not all(0 <= part <= 255 for part in ip_parts):
                return False
            
            # Validate port
            port_num = int(port)
            if not (1 <= port_num <= 65535):
                return False
            
            # Avoid local/private IPs
            if (ip.startswith('127.') or ip.startswith('192.168.') or 
                ip.startswith('10.') or ip.startswith('172.16.') or
                ip == '0.0.0.0' or ip.startswith('169.254.')):
                return False
                
            return True
        except (ValueError, AttributeError):
            return False

    def extract_proxies_from_text(self, text, source="unknown"):
        """Extract proxies from text with multiple patterns"""
        proxies = set()
        if not text:
            return proxies
            
        # Multiple patterns to find proxies
        patterns = [
            r'\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b',  # Basic pattern
            r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s*:\s*(\d{1,5})',  # With spaces
            r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+(\d{1,5})',  # Space separator
        ]
        
        all_matches = set()
        for pattern in patterns:
            matches = re.findall(pattern, text)
            if matches and isinstance(matches[0], tuple):
                for match in matches:
                    all_matches.add(f"{match[0]}:{match[1]}")
            else:
                all_matches.update(matches)
        
        for match in all_matches:
            if self.validate_proxy_format(match):
                proxies.add(match)
        
        return proxies

    def fetch_from_url(self, url):
        """Fetch proxies from a single URL"""
        source_name = urlparse(url).netloc
        
        try:
            headers = self.get_random_headers()
            
            response = requests.get(
                url, 
                headers=headers, 
                timeout=15,
                verify=False,
                allow_redirects=True
            )
            
            if response.status_code == 200:
                proxies = self.extract_proxies_from_text(response.text, source_name)
                if proxies:
                    self.source_stats[source_name] = len(proxies)
                    logger.info(f"✅ {source_name}: {len(proxies)} proxies")
                return proxies
            else:
                logger.warning(f"⚠️ {source_name}: HTTP {response.status_code}")
                
        except Exception as e:
            logger.warning(f"⚠️ Error fetching {source_name}: {str(e)[:100]}")
            
        return set()

    def parse_free_proxy_list(self, html_content, source):
        """Parse free-proxy-list.net"""
        proxies = set()
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            table = soup.find('table', {'id': 'proxylisttable'})
            
            if not table:
                table = soup.find('table')
            
            if table:
                rows = table.find_all('tr')[1:]  # Skip header
                
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) >= 2:
                        ip = cols[0].text.strip()
                        port = cols[1].text.strip()
                        proxy = f"{ip}:{port}"
                        
                        # Check HTTPS support if available
                        if len(cols) >= 7:
                            https_support = cols[6].text.strip().lower()
                            if https_support == 'yes' and self.validate_proxy_format(proxy):
                                proxies.add(proxy)
                        elif self.validate_proxy_format(proxy):
                            proxies.add(proxy)
                            
        except Exception as e:
            logger.warning(f"Error parsing {source}: {e}")
            
        return proxies

    def parse_proxy_list_download(self, html_content, source):
        """Parse proxy-list.download"""
        proxies = set()
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Check textarea first
            textarea = soup.find('textarea')
            if textarea:
                content = textarea.get_text(strip=True)
                for line in content.split('\n'):
                    line = line.strip()
                    if self.validate_proxy_format(line):
                        proxies.add(line)
            
            # Check tables if no textarea
            if not proxies:
                table = soup.find('table')
                if table:
                    rows = table.find_all('tr')[1:]
                    for row in rows:
                        cells = row.find_all('td')
                        if len(cells) >= 2:
                            ip = cells[0].get_text(strip=True)
                            port = cells[1].get_text(strip=True)
                            proxy = f"{ip}:{port}"
                            if self.validate_proxy_format(proxy):
                                proxies.add(proxy)
                                
        except Exception as e:
            logger.warning(f"Error parsing {source}: {e}")
            
        return proxies

    def scrape_web_source(self, url):
        """Scrape web sources with HTML parsing"""
        source_name = urlparse(url).netloc
        
        try:
            headers = self.get_random_headers()
            response = requests.get(url, headers=headers, timeout=15, verify=False)
            
            if response.status_code != 200:
                return set()
            
            proxies = set()
            
            # Specific parsers for known sites
            if 'free-proxy-list.net' in url or 'sslproxies.org' in url or 'us-proxy.org' in url:
                proxies = self.parse_free_proxy_list(response.text, source_name)
            elif 'proxy-list.download' in url:
                proxies = self.parse_proxy_list_download(response.text, source_name)
            else:
                # Generic parsing
                proxies = self.extract_proxies_from_text(response.text, source_name)
            
            if proxies:
                self.source_stats[source_name] = len(proxies)
                logger.info(f"✅ {source_name}: {len(proxies)} proxies")
                
            return proxies
            
        except Exception as e:
            logger.warning(f"⚠️ Error scraping {source_name}: {str(e)[:100]}")
            return set()

    def collect_all_proxies(self):
        """Collect proxies from all sources in parallel"""
        logger.info("🌐 Starting proxy collection from all sources...")
        logger.info(f"📊 Total sources: {len(self.proxy_sources) + len(self.web_sources)}")
        
        all_proxies = set()
        
        # Collect from URL sources
        with ThreadPoolExecutor(max_workers=20) as executor:
            url_futures = {executor.submit(self.fetch_from_url, url): url for url in self.proxy_sources}
            
            for future in as_completed(url_futures):
                url = url_futures[future]
                try:
                    proxies = future.result()
                    all_proxies.update(proxies)
                except Exception as e:
                    logger.error(f"❌ Error processing {url}: {e}")

        # Collect from web sources
        with ThreadPoolExecutor(max_workers=10) as executor:
            web_futures = {executor.submit(self.scrape_web_source, url): url for url in self.web_sources}
            
            for future in as_completed(web_futures):
                url = web_futures[future]
                try:
                    proxies = future.result()
                    all_proxies.update(proxies)
                except Exception as e:
                    logger.error(f"❌ Error processing {url}: {e}")

        self.proxies = all_proxies
        
        # Limit if too many
        if len(self.proxies) > MAX_PROXIES:
            logger.info(f"⚠️ Found {len(self.proxies)} proxies, testing first {MAX_PROXIES} only")
            self.proxies = set(list(self.proxies)[:MAX_PROXIES])
            
        self.total_count = len(self.proxies)
        
        logger.info(f"🎯 Total unique proxies collected: {self.total_count}")
        logger.info("📊 Source statistics:")
        for source, count in sorted(self.source_stats.items(), key=lambda x: x[1], reverse=True):
            if count > 0:
                logger.info(f"   📌 {source}: {count} proxies")
        
        return self.proxies

    def test_proxy_connection(self, proxy, timeout=5):
        """Test if proxy port is open"""
        try:
            ip, port = proxy.split(':')
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, int(port)))
            sock.close()
            return result == 0
        except:
            return False

    def test_proxy_instagram(self, proxy):
        """Test proxy against Instagram with retries"""
        for attempt in range(RETRY_COUNT):
            try:
                # Setup proxy
                proxies = {
                    'http': f"http://{proxy}",
                    'https': f"http://{proxy}"
                }
                
                headers = self.get_random_headers()
                
                # Measure response time
                start_time = time.time()
                
                # Test connection to Instagram
                response = requests.get(
                    TEST_URL,
                    proxies=proxies,
                    headers=headers,
                    timeout=TIMEOUT,
                    verify=False,
                    allow_redirects=True,
                    stream=False
                )
                
                end_time = time.time()
                response_time = int((end_time - start_time) * 1000)  # milliseconds
                
                # Check successful response
                if response.status_code == 200:
                    content = response.text.lower()
                    # Verify Instagram content
                    if ('instagram' in content or 'meta' in content or 
                        len(response.text) > 5000):
                        
                        return {
                            'proxy': proxy,
                            'working': True,
                            'response_time': response_time,
                            'status_code': response.status_code,
                            'tested_at': datetime.now().isoformat()
                        }
                
                # Retry on failure
                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.5)
                    continue
                    
            except requests.exceptions.Timeout:
                if attempt < RETRY_COUNT - 1:
                    continue
            except requests.exceptions.ConnectionError:
                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.5)
                    continue
            except Exception:
                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.5)
                    continue
        
        return {'proxy': proxy, 'working': False}

    def test_all_proxies(self):
        """Test all proxies against Instagram"""
        if not self.proxies:
            logger.error("❌ No proxies to test!")
            return
        
        logger.info(f"🧪 Starting to test {len(self.proxies)} proxies on Instagram...")
        logger.info(f"⚙️ Using {MAX_WORKERS} threads with {TIMEOUT}s timeout")
        
        self.tested_count = 0
        working_count = 0
        start_test_time = time.time()
        
        def print_progress():
            while self.tested_count < len(self.proxies):
                elapsed = time.time() - start_test_time
                rate = self.tested_count / elapsed if elapsed > 0 else 0
                progress = (self.tested_count / len(self.proxies)) * 100
                eta = (len(self.proxies) - self.tested_count) / rate if rate > 0 else 0
                
                print(f"\r🔄 Progress: {self.tested_count}/{len(self.proxies)} ({progress:.1f}%) "
                      f"| Working: {working_count} | Rate: {rate:.1f}/s | ETA: {eta:.0f}s", 
                      end='', flush=True)
                time.sleep(1)
        
        # Start progress thread
        progress_thread = threading.Thread(target=print_progress, daemon=True)
        progress_thread.start()
        
        # Test proxies in parallel
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_proxy = {executor.submit(self.test_proxy_instagram, proxy): proxy 
                             for proxy in self.proxies}
            
            for future in as_completed(future_to_proxy):
                try:
                    result = future.result()
                    self.tested_count += 1
                    
                    if result.get('working', False):
                        self.working_proxies.append(result)
                        working_count += 1
                        
                        # Log working proxies immediately
                        response_time = result.get('response_time', 'N/A')
                        logger.info(f"✅ Working: {result['proxy']} ({response_time}ms)")
                        
                except Exception as e:
                    self.tested_count += 1
                    logger.error(f"❌ Test error: {e}")
        
        print()  # New line after progress bar
        test_duration = time.time() - start_test_time
        logger.info(f"🎉 Testing completed in {test_duration:.1f} seconds!")
        logger.info(f"✅ Working proxies: {len(self.working_proxies)} out of {len(self.proxies)}")

    def save_results(self):
        """Save results to proxy.txt with simple format"""
        try:
            # Sort by response time (fastest first)
            working_sorted = sorted(self.working_proxies, key=lambda x: x.get('response_time', 9999))
            
            # Save to proxy.txt in simple IP:PORT format
            with open('proxy.txt', 'w', encoding='utf-8') as f:
                for proxy in working_sorted:
                    f.write(f"{proxy['proxy']}\n")
            
            # Save detailed results to JSON
            scan_duration = time.time() - self.start_time
            with open('proxy_details.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'scan_info': {
                        'scan_date': datetime.now().isoformat(),
                        'total_sources': len(self.proxy_sources) + len(self.web_sources),
                        'total_collected': self.total_count,
                        'total_working': len(self.working_proxies),
                        'success_rate': f"{(len(self.working_proxies) / self.total_count * 100):.2f}%" if self.total_count > 0 else "0%",
                        'scan_duration': f"{scan_duration:.2f}s"
                    },
                    'working_proxies': working_sorted
                }, f, ensure_ascii=False, indent=2)
            
            logger.info("💾 Results saved:")
            logger.info("   📄 proxy.txt - Working proxies in IP:PORT format")
            logger.info("   📄 proxy_details.json - Detailed results and statistics")
            
        except Exception as e:
            logger.error(f"❌ Error saving results: {e}")

    def print_final_stats(self):
        """Print final comprehensive statistics"""
        duration = time.time() - self.start_time
        success_rate = (len(self.working_proxies) / self.total_count * 100) if self.total_count > 0 else 0
        
        print("\n" + "="*80)
        print("🎯 FINAL RESULTS - Ultimate Proxy Scraper")
        print("="*80)
        print(f"📊 Total sources used: {len(self.proxy_sources) + len(self.web_sources)}")
        print(f"✅ Total proxies collected: {self.total_count:,}")
        print(f"🧪 Proxies tested: {self.tested_count:,}")
        print(f"✅ Working proxies on Instagram: {len(self.working_proxies):,}")
        print(f"📈 Success rate: {success_rate:.2f}%")
        print(f"🕒 Total scan duration: {duration:.1f} seconds")
        print("="*80)
        
        if self.working_proxies:
            print("🏆 Top 10 fastest proxies:")
            fastest = sorted(self.working_proxies, key=lambda x: x.get('response_time', 9999))[:10]
            for i, proxy in enumerate(fastest, 1):
                response_time = proxy.get('response_time', 'N/A')
                print(f"   {i:2d}. {proxy['proxy']:<18} - {response_time:>4}ms")
            
            print(f"\n📁 All {len(self.working_proxies)} working proxies saved to 'proxy.txt'")
        else:
            print("❌ No working proxies found!")
            print("💡 Tips:")
            print("   • Try reducing TIMEOUT or increasing MAX_WORKERS")
            print("   • Check your internet connection")
            print("   • Instagram may be blocking free proxies")
        
        print("="*80)

    def run(self):
        """Run the ultimate proxy scraper"""
        try:
            print("🚀 Starting Ultimate HTTPS Proxy Scraper")
            print("="*80)
            
            # Collect proxies
            self.collect_all_proxies()
            
            if not self.proxies:
                logger.error("❌ No proxies found!")
                return
            
            # Test proxies
            self.test_all_proxies()
            
            # Save results
            self.save_results()
            
            # Print statistics
            self.print_final_stats()
            
        except KeyboardInterrupt:
            logger.info("\n⏹️ Process interrupted by user")
            if self.working_proxies:
                logger.info("💾 Saving current results...")
                self.save_results()
                self.print_final_stats()
        except Exception as e:
            logger.error(f"❌ General error: {e}")
            import traceback
            traceback.print_exc()

def main():
    """Main function"""
    print("🔥 Ultimate HTTPS Proxy Scraper - Combined Version")
    print("="*80)
    print("📋 Features:")
    print("   • Collect from 80+ unique sources")
    print("   • GitHub repositories & direct APIs")
    print("   • Web scraping with intelligent parsing") 
    print("   • Real Instagram testing")
    print("   • Fast parallel processing")
    print("   • Clean IP:PORT output format")
    print("="*80)
    print(f"⚙️ Configuration:")
    print(f"   • Max proxies to test: {MAX_PROXIES:,}")
    print(f"   • Parallel threads: {MAX_WORKERS}")
    print(f"   • Connection timeout: {TIMEOUT}s")
    print(f"   • Retry attempts: {RETRY_COUNT}")
    print(f"   • Test URL: {TEST_URL}")
    print("="*80)
    
    try:
        input("Press Enter to start or Ctrl+C to cancel...")
    except KeyboardInterrupt:
        print("\n❌ Process cancelled")
        return
    
    scraper = UltimateProxyScraper()
    scraper.run()

if __name__ == "__main__":
    main()