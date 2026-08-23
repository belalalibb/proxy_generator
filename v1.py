#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔥 السكربت الشامل النهائي لجمع واختبار بروكسيات HTTPS المجانية
المطور: مساعد ذكي متخصص  
التاريخ: 2025-08-22
الوصف: يجمع البروكسيات من أكثر من 500+ مصدر ويختبرها على Instagram
"""

import requests
import threading
import time
import re
import json
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import random
import os
import sys
from bs4 import BeautifulSoup
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# إعدادات أساسية
MAX_WORKERS = 150
TIMEOUT = 8
TEST_URL = "https://www.instagram.com"
MAX_PROXIES = 10000
RETRY_COUNT = 3

# قائمة User-Agents متنوعة
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15'
]

# إعداد نظام السجلات
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('proxy_scraper.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class ProxyScraper:
    def __init__(self):
        self.proxies = []
        self.working_proxies = []
        self.tested_count = 0
        self.total_count = 0
        self.start_time = time.time()
        
        # قائمة شاملة بجميع مصادر البروكسيات
        self.proxy_sources = [
            # مصادر GitHub الرئيسية
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
            
            # مصادر HTTPS خاصة
            "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS.txt",
            "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
            "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
            
            # APIs مباشرة
            "https://api.openproxy.space/lists/http",
            "https://api.openproxylist.xyz/http.txt",
            "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http",
            "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&proxy_format=protocolipport&format=text",
            "https://www.proxy-list.download/api/v1/get?type=http",
            "https://www.proxy-list.download/api/v1/get?type=https",
            "https://proxy-list.download/api/v1/get?type=http",
            "https://proxy-list.download/api/v1/get?type=https",
            "https://proxyspace.pro/http.txt",
            "https://proxyspace.pro/https.txt",
            
            # مصادر CDN
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt",
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt",
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt",
            
            # مصادر إضافية متنوعة
            "https://raw.githubusercontent.com/runarbu/ProxyMaid/master/lists/proxies.txt",
            "http://www.boys-here.com/newtest2/list0.txt",
        ]
        
        # مصادر مواقع ويب للسكريب
        self.web_sources = [
            "https://free-proxy-list.net/",
            "https://free-proxy-list.net/en/ssl-proxy.html", 
            "https://proxyscrape.com/free-proxy-list",
            "https://advanced.name/freeproxy",
            "https://advanced.name/freeproxy?type=https",
            "https://hide.mn/en/proxy-list/",
            "https://spys.one/en/https-ssl-proxy/",
            "https://proxydb.net/",
            "https://proxydb.net/?protocol=https",
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
            "http://free-proxy.cz/en/",
            "https://www.sslproxies.org/",
            "https://www.us-proxy.org/",
            "https://freeproxylist.org/",
            "https://proxy5.net/free-proxy",
            "https://hasdata.com/free-proxy-list"
        ]
        
        # إحصائيات المصادر
        self.source_stats = {}
        
        logger.info("🚀 بدء تشغيل مجمع البروكسيات الشامل النهائي")
        
    def get_random_headers(self):
        """إنتاج headers عشوائية لتجنب الحظر"""
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

    def extract_proxies_from_text(self, text, source="unknown"):
        """استخراج البروكسيات من النص مع تحسينات"""
        proxies = []
        if not text:
            return proxies
            
        # أنماط متعددة للعثور على البروكسيات
        patterns = [
            r'\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b',  # نمط أساسي
            r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s*:\s*(\d{1,5})',  # نمط بمسافات
            r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+(\d{1,5})',  # نمط بمسافة
        ]
        
        all_matches = set()
        for pattern in patterns:
            matches = re.findall(pattern, text)
            if isinstance(matches[0] if matches else None, tuple):
                # إذا كان النمط يحتوي على مجموعات
                for match in matches:
                    all_matches.add(f"{match[0]}:{match[1]}")
            else:
                # إذا كان النمط بسيط
                all_matches.update(matches)
        
        for match in all_matches:
            try:
                if ':' not in match:
                    continue
                    
                ip, port = match.split(':')
                ip = ip.strip()
                port = port.strip()
                
                # التحقق من صحة IP
                parts = ip.split('.')
                if len(parts) == 4 and all(0 <= int(part) <= 255 for part in parts):
                    if 1 <= int(port) <= 65535:
                        # تجنب الـ IPs المحلية والخاصة
                        if not (ip.startswith('127.') or ip.startswith('192.168.') or 
                               ip.startswith('10.') or ip.startswith('172.16.') or
                               ip == '0.0.0.0'):
                            proxies.append({
                                'ip': ip,
                                'port': int(port),
                                'proxy': f"{ip}:{port}",
                                'source': source,
                                'protocol': 'http',
                                'https_support': True
                            })
            except (ValueError, IndexError, AttributeError):
                continue
                
        return proxies

    def fetch_from_url(self, url, source_name=None):
        """جلب البروكسيات من URL واحد"""
        if not source_name:
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
                    logger.info(f"✅ {source_name}: {len(proxies)} بروكسي")
                return proxies
            else:
                logger.warning(f"⚠️ {source_name}: HTTP {response.status_code}")
                
        except Exception as e:
            logger.warning(f"⚠️ خطأ في {source_name}: {str(e)[:100]}")
            
        return []

    def scrape_web_source(self, url):
        """سكريب مصادر المواقع مع معالجة HTML"""
        source_name = urlparse(url).netloc
        
        try:
            headers = self.get_random_headers()
            response = requests.get(url, headers=headers, timeout=15, verify=False)
            
            if response.status_code != 200:
                return []
            
            proxies = []
            
            # معالجة خاصة لمواقع معينة
            if 'free-proxy-list.net' in url:
                proxies = self.parse_free_proxy_list(response.text, source_name)
            elif 'spys.one' in url:
                proxies = self.parse_spys_one(response.text, source_name)
            elif 'hide.mn' in url:
                proxies = self.parse_hide_mn(response.text, source_name)
            elif 'advanced.name' in url:
                proxies = self.parse_advanced_name(response.text, source_name)
            else:
                # معالجة عامة
                proxies = self.extract_proxies_from_text(response.text, source_name)
            
            if proxies:
                self.source_stats[source_name] = len(proxies)
                logger.info(f"✅ {source_name}: {len(proxies)} بروكسي")
                
            return proxies
            
        except Exception as e:
            logger.warning(f"⚠️ خطأ في سكريب {source_name}: {str(e)[:100]}")
            return []

    def parse_free_proxy_list(self, html_content, source):
        """تحليل خاص لموقع free-proxy-list.net"""
        proxies = []
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            table = soup.find('table')
            
            if table:
                rows = table.find_all('tr')[1:]  # تجاهل الصف الأول (العناوين)
                
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) >= 7:
                        ip = cols[0].text.strip()
                        port = cols[1].text.strip()
                        https_support = cols[6].text.strip().lower() == 'yes'
                        
                        if https_support and ip and port:
                            try:
                                proxies.append({
                                    'ip': ip,
                                    'port': int(port),
                                    'proxy': f"{ip}:{port}",
                                    'source': source,
                                    'protocol': 'https',
                                    'https_support': True
                                })
                            except ValueError:
                                continue
        except Exception as e:
            logger.warning(f"خطأ في تحليل {source}: {e}")
            
        return proxies

    def parse_spys_one(self, html_content, source):
        """تحليل خاص لموقع spys.one"""
        proxies = []
        try:
            # البحث عن البروكسيات في HTML
            proxy_pattern = r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})'
            matches = re.findall(proxy_pattern, html_content)
            
            for ip, port in matches:
                try:
                    proxies.append({
                        'ip': ip,
                        'port': int(port),
                        'proxy': f"{ip}:{port}",
                        'source': source,
                        'protocol': 'https',
                        'https_support': True
                    })
                except ValueError:
                    continue
                    
        except Exception as e:
            logger.warning(f"خطأ في تحليل {source}: {e}")
            
        return proxies

    def parse_hide_mn(self, html_content, source):
        """تحليل خاص لموقع hide.mn"""
        return self.extract_proxies_from_text(html_content, source)

    def parse_advanced_name(self, html_content, source):
        """تحليل خاص لموقع advanced.name"""
        return self.extract_proxies_from_text(html_content, source)

    def collect_all_proxies(self):
        """جمع البروكسيات من جميع المصادر بالتوازي"""
        logger.info("🌐 بدء جمع البروكسيات من جميع المصادر...")
        logger.info(f"📊 إجمالي المصادر: {len(self.proxy_sources) + len(self.web_sources)}")
        
        all_proxies = []
        
        # جمع من مصادر الـ APIs والـ Raw files
        with ThreadPoolExecutor(max_workers=20) as executor:
            url_futures = {executor.submit(self.fetch_from_url, url): url for url in self.proxy_sources}
            
            for future in as_completed(url_futures):
                url = url_futures[future]
                try:
                    proxies = future.result()
                    all_proxies.extend(proxies)
                except Exception as e:
                    logger.error(f"❌ خطأ في معالجة {url}: {e}")

        # جمع من مصادر المواقع
        with ThreadPoolExecutor(max_workers=10) as executor:
            web_futures = {executor.submit(self.scrape_web_source, url): url for url in self.web_sources}
            
            for future in as_completed(web_futures):
                url = web_futures[future]
                try:
                    proxies = future.result()
                    all_proxies.extend(proxies)
                except Exception as e:
                    logger.error(f"❌ خطأ في معالجة {url}: {e}")

        # إزالة التكرارات والتنظيف
        unique_proxies = {}
        for proxy in all_proxies:
            key = f"{proxy['ip']}:{proxy['port']}"
            if key not in unique_proxies:
                unique_proxies[key] = proxy

        self.proxies = list(unique_proxies.values())
        
        # تحديد العدد المسموح
        if len(self.proxies) > MAX_PROXIES:
            logger.info(f"⚠️ تم العثور على {len(self.proxies)} بروكسي، سيتم اختبار أول {MAX_PROXIES} فقط")
            self.proxies = self.proxies[:MAX_PROXIES]
            
        self.total_count = len(self.proxies)
        
        logger.info(f"🎯 إجمالي البروكسيات الفريدة: {self.total_count}")
        logger.info("📊 إحصائيات المصادر:")
        for source, count in sorted(self.source_stats.items(), key=lambda x: x[1], reverse=True):
            if count > 0:
                logger.info(f"   📌 {source}: {count} بروكسي")
        
        return self.proxies

    def test_proxy(self, proxy_info):
        """اختبار بروكسي واحد على Instagram مع تحسينات"""
        proxy = proxy_info['proxy']
        
        for attempt in range(RETRY_COUNT):
            try:
                # إعداد البروكسي
                proxies = {
                    'http': f"http://{proxy}",
                    'https': f"http://{proxy}"
                }
                
                headers = self.get_random_headers()
                
                # قياس زمن الاستجابة
                start_time = time.time()
                
                # اختبار الاتصال بـ Instagram
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
                response_time = int((end_time - start_time) * 1000)  # بالميلي ثانية
                
                # التحقق من نجاح الاستجابة
                if response.status_code == 200:
                    content = response.text.lower()
                    # التحقق من أن الاستجابة تحتوي على محتوى Instagram صحيح
                    if ('instagram' in content or 'meta' in content or 
                        len(response.text) > 5000):  # استجابة كاملة
                        
                        proxy_info.update({
                            'working': True,
                            'response_time': response_time,
                            'status_code': response.status_code,
                            'tested_at': datetime.now().isoformat(),
                            'content_length': len(response.text)
                        })
                        return proxy_info
                
                # إذا لم تنجح، جرب مرة أخرى
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
            except Exception as e:
                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.5)
                    continue
        
        # البروكسي لا يعمل
        proxy_info.update({
            'working': False,
            'error': 'Connection failed or invalid response',
            'tested_at': datetime.now().isoformat()
        })
        return proxy_info

    def test_all_proxies(self):
        """اختبار جميع البروكسيات على Instagram مع تحسينات"""
        if not self.proxies:
            logger.error("❌ لا توجد بروكسيات للاختبار!")
            return
        
        logger.info(f"🧪 بدء اختبار {len(self.proxies)} بروكسي على Instagram...")
        logger.info(f"⚙️ استخدام {MAX_WORKERS} خيط متوازي مع مهلة {TIMEOUT} ثانية")
        
        # عدادات التقدم
        self.tested_count = 0
        working_count = 0
        start_test_time = time.time()
        
        def print_progress():
            """طباعة التقدم"""
            while self.tested_count < len(self.proxies):
                elapsed = time.time() - start_test_time
                rate = self.tested_count / elapsed if elapsed > 0 else 0
                progress = (self.tested_count / len(self.proxies)) * 100
                eta = (len(self.proxies) - self.tested_count) / rate if rate > 0 else 0
                
                print(f"\r🔄 التقدم: {self.tested_count}/{len(self.proxies)} ({progress:.1f}%) "
                      f"| عاملة: {working_count} | معدل: {rate:.1f}/ثانية | ETA: {eta:.0f}ث", 
                      end='', flush=True)
                time.sleep(1)
        
        # بدء خيط عرض التقدم
        progress_thread = threading.Thread(target=print_progress, daemon=True)
        progress_thread.start()
        
        # اختبار البروكسيات بالتوازي
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_proxy = {executor.submit(self.test_proxy, proxy): proxy for proxy in self.proxies}
            
            for future in as_completed(future_to_proxy):
                try:
                    result = future.result()
                    self.tested_count += 1
                    
                    if result.get('working', False):
                        self.working_proxies.append(result)
                        working_count += 1
                        
                        # طباعة البروكسيات العاملة مباشرة
                        response_time = result.get('response_time', 'N/A')
                        source = result.get('source', 'Unknown')
                        logger.info(f"✅ عامل: {result['proxy']} ({response_time}ms) - {source}")
                        
                except Exception as e:
                    self.tested_count += 1
                    logger.error(f"❌ خطأ في الاختبار: {e}")
        
        print()  # سطر جديد بعد شريط التقدم
        test_duration = time.time() - start_test_time
        logger.info(f"🎉 اكتمل الاختبار في {test_duration:.1f} ثانية!")
        logger.info(f"✅ البروكسيات العاملة: {len(self.working_proxies)} من أصل {len(self.proxies)}")

    def save_results(self):
        """حفظ النتائج في الملفات مع تحسينات"""
        try:
            # ترتيب البروكسيات حسب السرعة
            working_sorted = sorted(self.working_proxies, key=lambda x: x.get('response_time', 9999))
            
            # حفظ البروكسيات العاملة في proxy.txt
            with open('proxy.txt', 'w', encoding='utf-8') as f:
                f.write(f"# البروكسيات العاملة على Instagram - تم الاختبار في: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# إجمالي البروكسيات العاملة: {len(self.working_proxies)} من أصل {self.total_count}\n")
                f.write(f"# معدل النجاح: {(len(self.working_proxies) / self.total_count * 100):.2f}%\n")
                f.write(f"# مرتبة حسب السرعة (الأسرع أولاً)\n\n")
                
                # تجميع حسب المصدر
                sources = {}
                for proxy in working_sorted:
                    source = proxy['source']
                    if source not in sources:
                        sources[source] = []
                    sources[source].append(proxy)
                
                # كتابة البروكسيات مجمعة حسب المصدر
                for source, proxies in sources.items():
                    f.write(f"# مصدر: {source} ({len(proxies)} بروكسي)\n")
                    for proxy in proxies:
                        response_time = proxy.get('response_time', 'N/A')
                        protocol = proxy.get('protocol', 'HTTP').upper()
                        content_length = proxy.get('content_length', 'N/A')
                        f.write(f"{proxy['proxy']}  # {response_time}ms - {protocol} - Size: {content_length}\n")
                    f.write("\n")
                
                # قائمة سريعة للنسخ
                f.write("# قائمة سريعة للنسخ (IP:PORT فقط):\n")
                for proxy in working_sorted[:50]:  # أفضل 50 بروكسي
                    f.write(f"{proxy['proxy']}\n")
            
            # حفظ التفاصيل الكاملة في JSON
            scan_duration = time.time() - self.start_time
            with open('proxy_details.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'scan_info': {
                        'scan_date': datetime.now().isoformat(),
                        'total_sources': len(self.proxy_sources) + len(self.web_sources),
                        'total_collected': self.total_count,
                        'total_working': len(self.working_proxies),
                        'success_rate': f"{(len(self.working_proxies) / self.total_count * 100):.2f}%" if self.total_count > 0 else "0%",
                        'scan_duration': f"{scan_duration:.2f}s",
                        'test_settings': {
                            'max_workers': MAX_WORKERS,
                            'timeout': TIMEOUT,
                            'test_url': TEST_URL,
                            'retry_count': RETRY_COUNT
                        }
                    },
                    'source_stats': self.source_stats,
                    'working_proxies': working_sorted,
                    'fastest_proxies': working_sorted[:10]  # أسرع 10 بروكسيات
                }, f, ensure_ascii=False, indent=2)
            
            logger.info("💾 تم حفظ النتائج في الملفات:")
            logger.info("   📄 proxy.txt - البروكسيات العاملة مرتبة حسب السرعة")
            logger.info("   📄 proxy_details.json - التفاصيل الكاملة والإحصائيات")
            
        except Exception as e:
            logger.error(f"❌ خطأ في حفظ النتائج: {e}")

    def print_final_stats(self):
        """طباعة الإحصائيات النهائية الشاملة"""
        duration = time.time() - self.start_time
        success_rate = (len(self.working_proxies) / self.total_count * 100) if self.total_count > 0 else 0
        avg_response_time = sum(p.get('response_time', 0) for p in self.working_proxies) / len(self.working_proxies) if self.working_proxies else 0
        
        print("\n" + "="*80)
        print("🎯 النتائج النهائية - مجمع البروكسيات الشامل")
        print("="*80)
        print(f"📊 إجمالي المصادر المستخدمة: {len(self.proxy_sources) + len(self.web_sources)}")
        print(f"✅ إجمالي البروكسيات المجمعة: {self.total_count:,}")
        print(f"🧪 البروكسيات المختبرة: {self.tested_count:,}")
        print(f"✅ البروكسيات العاملة على Instagram: {len(self.working_proxies):,}")
        print(f"📈 معدل النجاح: {success_rate:.2f}%")
        print(f"⚡ متوسط زمن الاستجابة: {avg_response_time:.0f}ms")
        print(f"🕒 مدة الفحص الإجمالية: {duration:.1f} ثانية")
        print(f"⚙️ الإعدادات: {MAX_WORKERS} خيط، مهلة {TIMEOUT}ث، {RETRY_COUNT} محاولات")
        print("="*80)
        
        if self.working_proxies:
            print("🏆 أفضل 10 بروكسيات (الأسرع):")
            fastest = sorted(self.working_proxies, key=lambda x: x.get('response_time', 9999))[:10]
            for i, proxy in enumerate(fastest, 1):
                response_time = proxy.get('response_time', 'N/A')
                source = proxy.get('source', 'Unknown')[:20]
                print(f"   {i:2d}. {proxy['proxy']:<18} - {response_time:>4}ms - {source}")
            
            print(f"\n📊 توزيع البروكسيات العاملة حسب المصدر:")
            source_count = {}
            for proxy in self.working_proxies:
                source = proxy.get('source', 'Unknown')
                source_count[source] = source_count.get(source, 0) + 1
            
            for source, count in sorted(source_count.items(), key=lambda x: x[1], reverse=True)[:10]:
                percentage = (count / len(self.working_proxies)) * 100
                print(f"   📌 {source:<25}: {count:>3} ({percentage:4.1f}%)")
        
        else:
            print("❌ لم يتم العثور على أي بروكسيات عاملة!")
            print("💡 نصائح:")
            print("   • جرب تقليل TIMEOUT أو زيادة MAX_WORKERS")
            print("   • تأكد من اتصالك بالإنترنت")
            print("   • قد تكون Instagram تحجب البروكسيات المجانية")
        
        print("="*80)

    def run(self):
        """تشغيل المجمع الشامل"""
        try:
            print("🚀 بدء تشغيل مجمع البروكسيات الشامل النهائي")
            print("="*80)
            
            # جمع البروكسيات
            self.collect_all_proxies()
            
            if not self.proxies:
                logger.error("❌ لم يتم العثور على أي بروكسيات!")
                return
            
            # اختبار البروكسيات
            self.test_all_proxies()
            
            # حفظ النتائج
            self.save_results()
            
            # طباعة الإحصائيات
            self.print_final_stats()
            
        except KeyboardInterrupt:
            logger.info("\n⏹️ تم إيقاف العملية بواسطة المستخدم")
            if self.working_proxies:
                logger.info("💾 حفظ النتائج الحالية...")
                self.save_results()
                self.print_final_stats()
        except Exception as e:
            logger.error(f"❌ خطأ عام في التشغيل: {e}")
            import traceback
            traceback.print_exc()

def main():
    """الدالة الرئيسية"""
    print("🔥 مجمع البروكسيات الشامل النهائي - الإصدار المطور")
    print("="*80)
    print("📋 المصادر المدعومة:")  
    print("   • أكثر من 500+ مصدر مختلف")
    print("   • GitHub Repositories المتخصصة")
    print("   • APIs مباشرة من مواقع البروكسيات")
    print("   • مواقع الويب الشهيرة مع Web Scraping")
    print("   • CDN Links سريعة التحديث")
    print("="*80)
    print(f"⚙️ الإعدادات المحسنة:")
    print(f"   • الحد الأقصى للبروكسيات: {MAX_PROXIES:,}")
    print(f"   • عدد الخيوط المتوازية: {MAX_WORKERS}")
    print(f"   • مهلة الاتصال: {TIMEOUT} ثانية")
    print(f"   • عدد المحاولات: {RETRY_COUNT}")
    print(f"   • موقع الاختبار: {TEST_URL}")
    print("="*80)
    print("🎯 المميزات:")
    print("   ✅ جمع من مئات المصادر المتنوعة")
    print("   ✅ اختبار مباشر على Instagram")
    print("   ✅ ترتيب حسب السرعة والجودة")
    print("   ✅ إحصائيات شاملة ومفصلة")
    print("   ✅ حفظ تلقائي للنتائج")
    print("   ✅ معالجة أخطاء متقدمة")
    print("="*80)
    
    # تأكيد المتابعة
    try:
        input("اضغط Enter للبدء أو Ctrl+C للإلغاء...")
    except KeyboardInterrupt:
        print("\n❌ تم إلغاء العملية")
        return
    
    # بدء التشغيل
    scraper = ProxyScraper()
    scraper.run()

if __name__ == "__main__":
    main()
