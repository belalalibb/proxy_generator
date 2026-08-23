import requests
from bs4 import BeautifulSoup
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import socket
from urllib.parse import urljoin

class ProxyScraper:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive'
        }
        
        # List of free proxy sources
        self.proxy_sources = [
            {
                'name': 'Proxy-List.Download',
                'url': 'https://www.proxy-list.download/HTTPS',
                'parser': self.parse_proxy_list_download
            },
            {
                'name': 'ProxyDB',
                'url': 'https://proxydb.net/?protocol=https&country=',
                'parser': self.parse_proxydb
            },
            {
                'name': 'Free-Proxy-List',
                'url': 'https://free-proxy-list.net/',
                'parser': self.parse_free_proxy_list
            },
            {
                'name': 'ProxyScrape',
                'url': 'https://api.proxyscrape.com/v2/?request=get&protocol=http&timeout=10000&country=all&ssl=yes&anonymity=all',
                'parser': self.parse_proxyscrape_api
            },
            {
                'name': 'GeoNode',
                'url': 'https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=https',
                'parser': self.parse_geonode_api
            },
            {
                'name': 'Proxy11',
                'url': 'https://www.proxy11.com/api/demoweb/proxy.json?key=free-demo&country=&city=&port=&type=https',
                'parser': self.parse_proxy11_api
            },
            {
                'name': 'OpenProxyList',
                'url': 'https://openproxy.space/list/https',
                'parser': self.parse_openproxy
            },
            {
                'name': 'ProxyRotator',
                'url': 'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/https.txt',
                'parser': self.parse_raw_list
            },
            {
                'name': 'ProxySpace',
                'url': 'https://api.openproxylist.xyz/https.txt',
                'parser': self.parse_raw_list
            },
            {
                'name': 'Proxies-24',
                'url': 'https://www.proxies24.com/proxy-list/https-proxies',
                'parser': self.parse_proxies24
            }
        ]
        
        self.all_proxies = set()
        self.valid_proxies = []

    def validate_proxy_format(self, proxy):
        """Validate proxy IP:port format"""
        try:
            if ':' not in proxy:
                return False
            
            ip, port = proxy.split(':')
            
            # Validate IP
            ip_parts = [int(x) for x in ip.split('.')]
            if len(ip_parts) != 4 or not all(0 <= part <= 255 for part in ip_parts):
                return False
            
            # Validate port
            port_num = int(port)
            if not (1 <= port_num <= 65535):
                return False
                
            return True
        except (ValueError, AttributeError):
            return False

    def extract_proxies_from_text(self, text):
        """Extract proxy patterns from text"""
        proxy_pattern = r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})\b'
        matches = re.findall(proxy_pattern, text)
        proxies = []
        
        for ip, port in matches:
            proxy = f"{ip}:{port}"
            if self.validate_proxy_format(proxy):
                proxies.append(proxy)
        
        return proxies

    def parse_proxy_list_download(self, response):
        """Parse proxy-list.download"""
        soup = BeautifulSoup(response.content, 'html.parser')
        proxies = []
        
        # Check textarea
        textarea = soup.find('textarea')
        if textarea:
            content = textarea.get_text(strip=True)
            for line in content.split('\n'):
                line = line.strip()
                if self.validate_proxy_format(line):
                    proxies.append(line)
        
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
                            proxies.append(proxy)
        
        return proxies

    def parse_proxydb(self, response):
        """Parse ProxyDB"""
        soup = BeautifulSoup(response.content, 'html.parser')
        proxies = []
        
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
                        proxies.append(proxy)
        
        return proxies

    def parse_free_proxy_list(self, response):
        """Parse free-proxy-list.net"""
        soup = BeautifulSoup(response.content, 'html.parser')
        proxies = []
        
        table = soup.find('table', {'id': 'proxylisttable'})
        if table:
            rows = table.find_all('tr')[1:]
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 7:
                    # Check if HTTPS is supported (column 6)
                    https_support = cells[6].get_text(strip=True).lower()
                    if https_support == 'yes':
                        ip = cells[0].get_text(strip=True)
                        port = cells[1].get_text(strip=True)
                        proxy = f"{ip}:{port}"
                        if self.validate_proxy_format(proxy):
                            proxies.append(proxy)
        
        return proxies

    def parse_proxyscrape_api(self, response):
        """Parse ProxyScrape API response"""
        try:
            content = response.text.strip()
            proxies = []
            for line in content.split('\n'):
                line = line.strip()
                if self.validate_proxy_format(line):
                    proxies.append(line)
            return proxies
        except:
            return []

    def parse_geonode_api(self, response):
        """Parse GeoNode API response"""
        try:
            data = response.json()
            proxies = []
            if 'data' in data:
                for item in data['data']:
                    if item.get('protocols', []):
                        ip = item.get('ip')
                        port = item.get('port')
                        if ip and port:
                            proxy = f"{ip}:{port}"
                            if self.validate_proxy_format(proxy):
                                proxies.append(proxy)
            return proxies
        except:
            return []

    def parse_proxy11_api(self, response):
        """Parse Proxy11 API response"""
        try:
            data = response.json()
            proxies = []
            if 'data' in data:
                for item in data['data']:
                    ip = item.get('ip')
                    port = item.get('port')
                    if ip and port:
                        proxy = f"{ip}:{port}"
                        if self.validate_proxy_format(proxy):
                            proxies.append(proxy)
            return proxies
        except:
            return []

    def parse_openproxy(self, response):
        """Parse OpenProxy response"""
        soup = BeautifulSoup(response.content, 'html.parser')
        proxies = []
        
        # Look for proxy patterns in the page
        text_content = soup.get_text()
        proxies = self.extract_proxies_from_text(text_content)
        
        return proxies

    def parse_raw_list(self, response):
        """Parse raw proxy list"""
        try:
            content = response.text.strip()
            proxies = []
            for line in content.split('\n'):
                line = line.strip()
                if self.validate_proxy_format(line):
                    proxies.append(line)
            return proxies
        except:
            return []

    def parse_proxies24(self, response):
        """Parse Proxies24"""
        soup = BeautifulSoup(response.content, 'html.parser')
        proxies = []
        
        # Look for table or list structure
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
                        proxies.append(proxy)
        
        # Fallback to text extraction
        if not proxies:
            text_content = soup.get_text()
            proxies = self.extract_proxies_from_text(text_content)
        
        return proxies

    def fetch_from_source(self, source):
        """Fetch proxies from a single source"""
        try:
            print(f"🔄 Fetching from {source['name']}...")
            response = requests.get(source['url'], headers=self.headers, timeout=15)
            response.raise_for_status()
            
            proxies = source['parser'](response)
            print(f"✅ {source['name']}: Found {len(proxies)} proxies")
            return proxies
            
        except Exception as e:
            print(f"❌ {source['name']}: Error - {str(e)}")
            return []

    def test_proxy_connection(self, proxy, timeout=5):
        """Test if proxy is responsive"""
        try:
            ip, port = proxy.split(':')
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, int(port)))
            sock.close()
            return result == 0
        except:
            return False

    def filter_and_validate_proxies(self, test_connection=False):
        """Filter and validate collected proxies"""
        print(f"\n🔍 Filtering {len(self.all_proxies)} unique proxies...")
        
        # Basic format validation
        format_valid = [p for p in self.all_proxies if self.validate_proxy_format(p)]
        print(f"✅ Format validation: {len(format_valid)} valid")
        
        if test_connection:
            print("🔗 Testing proxy connections (this may take a while)...")
            with ThreadPoolExecutor(max_workers=50) as executor:
                future_to_proxy = {executor.submit(self.test_proxy_connection, proxy): proxy 
                                 for proxy in format_valid}
                
                connection_valid = []
                for future in as_completed(future_to_proxy):
                    proxy = future_to_proxy[future]
                    try:
                        if future.result():
                            connection_valid.append(proxy)
                    except:
                        pass
            
            print(f"✅ Connection test: {len(connection_valid)} responsive")
            self.valid_proxies = sorted(connection_valid)
        else:
            self.valid_proxies = sorted(format_valid)

    def save_results(self, filename="cleaned_proxy_urls.txt"):
        """Save clean proxy list to file"""
        try:
            with open(filename, 'w') as f:
                for proxy in self.valid_proxies:
                    f.write(f"{proxy}\n")
            
            print(f"\n💾 Saved {len(self.valid_proxies)} clean proxies to {filename}")
            return True
        except Exception as e:
            print(f"❌ Error saving file: {e}")
            return False

    def run(self, test_connections=False):
        """Main execution method"""
        print("🚀 Starting multi-source HTTPS proxy collection...")
        print("=" * 60)
        
        # Fetch from all sources
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(self.fetch_from_source, source) 
                      for source in self.proxy_sources]
            
            for future in as_completed(futures):
                try:
                    proxies = future.result()
                    self.all_proxies.update(proxies)
                except Exception as e:
                    print(f"❌ Source error: {e}")
        
        print("\n" + "=" * 60)
        print(f"📊 Collection Summary:")
        print(f"   Total unique proxies found: {len(self.all_proxies)}")
        
        # Filter and validate
        self.filter_and_validate_proxies(test_connections)
        
        # Save results
        if self.save_results():
            print("\n" + "=" * 60)
            print("🎉 COMPLETE! Clean proxy list saved to 'cleaned_proxy_urls.txt'")
            print("=" * 60)
            
            # Display sample
            print("\nSample proxies:")
            for i, proxy in enumerate(self.valid_proxies[:10], 1):
                print(f"{i:2d}. {proxy}")
            
            if len(self.valid_proxies) > 10:
                print(f"    ... and {len(self.valid_proxies) - 10} more")
        
        return self.valid_proxies

def main():
    scraper = ProxyScraper()
    
    print("Choose validation level:")
    print("1. Format validation only (fast)")
    print("2. Format + connection testing (slow but more reliable)")
    
    try:
        choice = input("\nEnter choice (1 or 2): ").strip()
        test_connections = choice == '2'
    except:
        test_connections = False
    
    print()
    proxies = scraper.run(test_connections)
    
    print(f"\n✨ Final result: {len(proxies)} clean HTTPS proxies ready to use!")

if __name__ == "__main__":
    main()