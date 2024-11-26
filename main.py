import requests
import json
import redis
from time import sleep
from datetime import datetime
from bs4 import BeautifulSoup
import logging
import os
from typing import Dict, List, Optional
from urllib.parse import quote
import telegram
from dataclasses import dataclass, field

# Logger configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_CONFIG = {
    "REDIS_URL": "redis://localhost:6379",
    "TELEGRAM_TOKEN": "YOUR_DEFAULT_BOT_TOKEN",  # Change to your token
    "TELEGRAM_CHAT_ID": "YOUR_DEFAULT_CHAT_ID",  # Change to your chat_id
    "CHECK_INTERVAL": 300,  # 5 minutes
    "DEFAULT_SEARCHES": [
        ("DevOps Engineer", "Wroclaw"),
        ("Cloud Engineer", "Warszawa"),
        ("DevOps Engineer", None),  # None means all locations
    ]
}

@dataclass
class JobSearch:
    position: str
    city: Optional[str] = None
    
    def get_url(self) -> str:
        """Generate URL for job search."""
        base_url = "https://it.pracuj.pl/praca"
        position_encoded = quote(self.position)
        
        if self.city:
            city_encoded = quote(self.city)
            return f"{base_url}/{position_encoded};kw/{city_encoded};wp?rd=30"
        return f"{base_url}/{position_encoded};kw"
    
    def get_search_id(self) -> str:
        """Generate unique search identifier."""
        if self.city:
            return f"{self.position}:{self.city}".lower()
        return self.position.lower()

class JobMonitor:
    def __init__(self, redis_url: Optional[str] = None, telegram_token: Optional[str] = None, 
                 telegram_chat_id: Optional[str] = None, check_interval: Optional[int] = None):
        """
        Initialize job monitor.
        
        Args:
            redis_url: Redis URL (optional)
            telegram_token: Telegram bot token (optional)
            telegram_chat_id: Telegram chat ID (optional)
            check_interval: Check interval in seconds (optional)
        """
        self.redis_url = redis_url or os.getenv("REDIS_URL", DEFAULT_CONFIG["REDIS_URL"])
        self.telegram_token = telegram_token or os.getenv("TELEGRAM_TOKEN", DEFAULT_CONFIG["TELEGRAM_TOKEN"])
        self.chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", DEFAULT_CONFIG["TELEGRAM_CHAT_ID"])
        self.check_interval = check_interval or int(os.getenv("CHECK_INTERVAL", DEFAULT_CONFIG["CHECK_INTERVAL"]))
        
        # Redis initialization
        try:
            self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
            logger.info("Connected to Redis")
        except redis.RedisError as e:
            logger.error(f"Redis connection error: {e}")
            self.redis_client = None
        
        self.bot = telegram.Bot(token=self.telegram_token)
        self.searches = {}  # Using dictionary instead of set
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
        }

        self.setup()

    def setup(self):
        """Initialize default searches."""
        if self.redis_client:
            # Try to load saved searches from Redis
            saved_searches = self.redis_client.smembers("job_searches")
            
            if saved_searches:
                for search_str in saved_searches:
                    position, *city = search_str.split(":")
                    self.add_search(position, city[0] if city else None)
            else:
                # If no saved searches, use defaults
                for position, city in DEFAULT_CONFIG["DEFAULT_SEARCHES"]:
                    self.add_search(position, city)
        else:
            # If no Redis, use default searches
            for position, city in DEFAULT_CONFIG["DEFAULT_SEARCHES"]:
                self.add_search(position, city)
                
        logger.info(f"Initialized {len(self.searches)} searches")

    def add_search(self, position: str, city: Optional[str] = None) -> None:
        """Add new search."""
        search = JobSearch(position, city)
        search_id = search.get_search_id()
        self.searches[search_id] = search
        
        if self.redis_client:
            self.redis_client.sadd("job_searches", search_id)
        logger.info(f"Added new search: {search_id}")

    def remove_search(self, position: str, city: Optional[str] = None) -> None:
        """Remove search."""
        search = JobSearch(position, city)
        search_id = search.get_search_id()
        self.searches.pop(search_id, None)
        
        if self.redis_client:
            self.redis_client.srem("job_searches", search_id)
        logger.info(f"Removed search: {search_id}")

    def send_telegram_alert(self, job: Dict, search: JobSearch) -> None:
        """Send Telegram alert about new job offer."""
        try:
            message = (
                f"🚨 New job offer: {job['jobTitle']}!\n\n"
                f"🔍 Search: {search.position}"
                f"{f' in {search.city}' if search.city else ''}\n\n"
                f"🏢 Company: {job['companyName']}\n"
                f"📍 Location: {job['displayWorkplace']}\n"
                f"💼 Level: {', '.join(job['positionLevels'])}\n"
                f"🔧 Technologies: {', '.join(job['technologies'])}\n"
                f"💰 Salary: {job.get('salaryDisplayText', 'Not specified')}\n"
                f"🔗 Link: {job['offerAbsoluteUri']}\n\n"
                f"📅 Published: {job['lastPublicated']}"
            )
            
            self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='HTML'
            )
            logger.info(f"Sent alert for job offer: {job['jobTitle']}")
        except Exception as e:
            logger.error(f"Error sending Telegram alert: {str(e)}")

    def get_job_offers(self, search: JobSearch) -> List[Dict]:
        """Get job offers for given search."""
        try:
            url = search.get_url()
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            next_data = soup.find('script', {'id': '__NEXT_DATA__'})
            
            if not next_data:
                logger.error(f"No JSON data found on page: {url}")
                return []
                
            data = json.loads(next_data.string)
            offers = data['props']['pageProps']['data']['jobOffers']['groupedOffers']
            return offers
                
        except Exception as e:
            logger.error(f"Error getting offers for {search.get_search_id()}: {str(e)}")
            return []

    def process_offers(self, offers: List[Dict], search: JobSearch) -> None:
        """Process offers and send alerts for new ones."""
        for offer in offers:
            offer_id = f"{search.get_search_id()}:{offer['groupId']}"
            try:
                if not self.redis_client or not self.redis_client.exists(f"offer:{offer_id}"):
                    offer_data = {
                        'companyName': offer['companyName'],
                        'jobTitle': offer['jobTitle'],
                        'lastPublicated': offer['lastPublicated'],
                        'technologies': ','.join(offer['technologies']),
                        'displayWorkplace': offer['offers'][0]['displayWorkplace'],
                        'offerAbsoluteUri': offer['offers'][0]['offerAbsoluteUri'],
                        'positionLevels': ','.join(offer['positionLevels']),
                        'salaryDisplayText': offer.get('salaryDisplayText', '')
                    }
                    
                    if self.redis_client:
                        self.redis_client.hmset(f"offer:{offer_id}", offer_data)
                    
                    self.send_telegram_alert({
                        **offer_data,
                        'technologies': offer['technologies'],
                        'positionLevels': offer['positionLevels']
                    }, search)
            except Exception as e:
                logger.error(f"Error processing offer {offer_id}: {str(e)}")

    def run(self) -> None:
        """Start job monitoring."""
        logger.info("Started job monitoring...")
        
        while True:
            try:
                for search in self.searches.values():
                    offers = self.get_job_offers(search)
                    if offers:
                        self.process_offers(offers, search)
                        logger.info(f"Processed {len(offers)} offers for {search.get_search_id()}")
                    
                    sleep(1)  # Short pause between requests
                
                sleep(self.check_interval)
                
            except KeyboardInterrupt:
                logger.info("Monitoring stopped")
                break
            except Exception as e:
                logger.error(f"Error occurred: {str(e)}")
                sleep(self.check_interval)

def main():
    monitor = JobMonitor()
    monitor.run()

if __name__ == "__main__":
    main()