from anthropic import Anthropic
from dotenv import load_dotenv
import os
import json

load_dotenv()

client = Anthropic()

def analyze_market(market: dict, context: str = "") -> dict:
    """Send market to Claude and get structured analysis"""
    
    with open("part-1/prompts/system_prompt.txt") as f:
        system = f.read()
    
    with open("part-1/prompts/analysis_prompt.txt") as f:
        template = f.read()
    
    user_prompt = template.format(
        question=market['question'],
        description=market['description'],
        end_date=market.get('end_date', 'Unknown'),
    )
    
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        system=system,
        messages=[{"role": "user", "content": user_prompt}]
    )
    
    text = response.content[0].text.strip()
    
    # Extract JSON
    try:
        start = text.find('{')
        end = text.rfind('}') + 1
        json_str = text[start:end]
        return json.loads(json_str)
    except:
        return {"error": "Failed to parse JSON", "raw": text}