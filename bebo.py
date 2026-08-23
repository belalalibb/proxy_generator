import requests
import time
import os
def send_to_telegram(message,TELEGRAM_API_TOKEN,TELEGRAM_CHAT_ID):
    apiURL = f'https://api.telegram.org/bot{TELEGRAM_API_TOKEN}/sendMessage'
    try:
        response = requests.post(apiURL, json={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
    except Exception as e:
        pass
        #print(e)
def get_cap_id(api_key, site_key, page_url):
    captcha_id_response = requests.post(
        f"http://2captcha.com/in.php?key={api_key}&method=userrecaptcha&googlekey={site_key}&pageurl={page_url}"
    )
    
    captcha_id = captcha_id_response.text.split('|')[1]
    return captcha_id

def get_cap_sol(api_key,captcha_id):
    captcha_solution = None
    while captcha_solution is None:
        time.sleep(5)  
        result = requests.get(f"http://2captcha.com/res.php?key={api_key}&action=get&id={captcha_id}")
        if result.text.startswith("OK|"):
            captcha_solution = result.text.split('|')[1]
            return captcha_solution
        else:
            pass

def check_or_create_file(file_path):
    dir_name = os.path.dirname(file_path)
    if dir_name and not os.path.exists(dir_name):
        os.makedirs(dir_name)




def files_as_li(filename):
    with open(filename, "r") as cards_file:
        cards = [card for card in cards_file.read().split("\n")]
    return cards

def remove(value, file_path):
    lines = []
    with open(file_path, 'r') as file:
        lines = file.readlines()
    
    with open(file_path, 'w') as file:
        for line in lines:
            if line.strip() != value:
                file.write(line)

#=================store value in txt_file==================
def store_in_text(file_name, value):
    dir_name = os.path.dirname(file_name)
    if dir_name and not os.path.exists(dir_name):
        os.makedirs(dir_name)  # Create the directory if it doesn't exist

    with open(file_name, 'a') as file:
        file.write(value + '\n')

#===========================================================



#check_or_create_file("temp/example.txt")



#check_or_create_file("temp/example.txt")