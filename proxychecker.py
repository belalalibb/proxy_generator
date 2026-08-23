import requests
import bebo

# ANSI color codes
RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

def is_proxy_working(proxy_url):
    proxy_dict = {
        "http":  "http://" + proxy_url,
        "https": "http://" + proxy_url
    }
    try:
        response = requests.get("https://httpbin.org/ip", proxies=proxy_dict, timeout=5)
        print(response.text)
        return response.status_code == 200
    except:
        return False

proxy_li = bebo.files_as_li('proxy.txt')

for pr in proxy_li:
    proxy_li_clend = bebo.files_as_li('clend_proxy.txt')
    pr_url ="http://" + pr
    if is_proxy_working(pr) and pr_url not in proxy_li_clend:
        print(GREEN + pr + ' working' + RESET)
        bebo.store_in_text("clend_proxy.txt", "http://" + pr)
    else:
        print(RED + pr + ' not working' + RESET)
    #bebo.remove(pr,'proxy.txt')