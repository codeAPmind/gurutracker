from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class RawEvent:
    guru_name: str
    event_type: str          # "trade" | "speech" | "filing"
    source: str              # "sec_13f" | "ark_trades" | "x" | "xueqiu" | "congress" | "fmp_13f"
    raw_content: str
    timestamp: datetime
    url: Optional[str] = None
    metadata: Optional[dict] = field(default_factory=dict)


class BaseCollector(ABC):

    @abstractmethod
    def collect(self) -> list[RawEvent]:
        pass

    @abstractmethod
    def get_source_name(self) -> str:
        pass

    def safe_collect(self) -> list[RawEvent]:
        try:
            return self.collect()
        except Exception as e:
            logger.error(f"[{self.get_source_name()}] 采集失败: {e}", exc_info=True)
            return []
