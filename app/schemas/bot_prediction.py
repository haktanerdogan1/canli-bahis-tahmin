from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class BotPrediction(BaseModel):
    bot_name: str
    bot_version: str = "v1.0.0"
    match_id: str
    snapshot_id: Optional[int] = None
    prediction_time: datetime = Field(default_factory=datetime.utcnow)
    minute: int
    period: str
    market: str
    decision: str  # "goal", "no_goal", "insufficient_data"
    probability: Optional[float] = None
    confidence: str  # "low", "medium", "high"
    data_quality: float  # 0.0 to 1.0
    reasons: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    sample_size: Optional[int] = None
