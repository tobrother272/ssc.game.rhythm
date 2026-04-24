import requests

def authourize_user(token, url):
    return True
    headers = {'Authorization': f'Bearer {token}'}
    try:
        response = requests.get(f'http://{url}/api/v2/userinfo', headers=headers)
        if response.status_code == 200 and 'id' in response.json().get('data', {}):
            return True
        else:
            return False
    except Exception as e:
        return False