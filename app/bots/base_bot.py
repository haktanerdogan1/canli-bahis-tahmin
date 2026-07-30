from abc import ABC, abstractmethod
from app.schemas.bot_prediction import BotPrediction
from typing import Dict, Any

class BaseGoalBot(ABC):
    def __init__(self, name: str, version: str = "v1.0.0"):
        self.name = name
        self.version = version

    @abstractmethod
    def predict(self, match_context: Dict[str, Any]) -> BotPrediction:
        """
        Her bot bu arayüzü uygulamak zorundadır.
        match_context içerisinde şunlar olabilir:
        - db_cursor (veya veritabanı oturumu)
        - match_id
        - minute
        - period
        - skor
        """
        pass
