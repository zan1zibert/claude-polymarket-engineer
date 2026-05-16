import requests
import pandas as pd
from typing import List, Dict

def get_active_markets(limit: int = 100) -> List[Dict]:
    """Fetch currently active Polymarket markets using Gamma API"""
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "sort": "volume",
        "order": "desc",
        "end-date-max": ???
    }
    
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    
    markets = []
    for m in data:
        if m.get('tokens') and len(m['tokens']) > 0:
            yes_token = next((t for t in m['tokens'] if t['outcome'] == 'Yes'), None)
            if yes_token:
                markets.append({
                    'id': m['id'],
                    'question': m['question'],
                    'description': m.get('description', ''),
                    'yes_price': round(yes_token['price'] * 100, 2),
                    'volume': m.get('volume', 0),
                    'end_date': m.get('end_date'),
                    'slug': m.get('slug')
                })
    return markets