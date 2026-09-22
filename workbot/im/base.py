from __future__ import annotations

from abc import ABC, abstractmethod
from workbot.conversation.models import IncomingMessage


class IMAdapter(ABC):
    @abstractmethod
    async def poll(self) -> list[IncomingMessage]: ...

    @abstractmethod
    async def send_text(self, conversation_id: str, text: str) -> None: ...
