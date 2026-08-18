import asyncio
import os
from typing import Final

from agent_framework import Agent
from agent_framework.gemini import GeminiChatClient
from dotenv import load_dotenv


EXIT_COMMANDS: Final[set[str]] = {"exit", "quit", "cik", "çık"}

INSTRUCTIONS: Final[str] = (
    "Sen Türkçe konuşan yardımcı bir yapay zeka ajanısın. "
    "Kullanıcının sorularına açık, doğru ve anlaşılır cevaplar ver. "
    "Bilmediğin konularda bilgi uydurma."
)


def validate_environment() -> None:
    """Load and validate required environment variables."""
    load_dotenv(override=True)

    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY bulunamadı. .env dosyasını kontrol edin.")


def create_agent() -> Agent:
    return Agent(
        client=GeminiChatClient(),
        name="GeminiAgent",
        instructions=INSTRUCTIONS,
    )


def format_error(error: Exception) -> str:
    error_text = str(error).lower()

    if "api key" in error_text or "apikey" in error_text or "permission" in error_text:
        return "Gemini API anahtarı geçersiz veya yetkisiz görünüyor. .env dosyasını kontrol edin."

    if "rate limit" in error_text or "quota" in error_text or "429" in error_text:
        return "Gemini API rate limit veya kota sınırına ulaşıldı. Bir süre sonra tekrar deneyin."

    if "model" in error_text and (
        "not found" in error_text or "404" in error_text or "invalid" in error_text
    ):
        return "Gemini modeli bulunamadı veya kullanılamıyor. GEMINI_MODEL değerini kontrol edin."

    if (
        "connection" in error_text
        or "timeout" in error_text
        or "network" in error_text
        or "unavailable" in error_text
    ):
        return "Gemini API bağlantı hatası oluştu. İnternet bağlantınızı ve servis durumunu kontrol edin."

    return "Agent Framework veya Gemini API tarafında bir hata oluştu. Lütfen ayarları kontrol edip tekrar deneyin."


async def chat_loop(agent: Agent) -> None:
    print("Gemini Agent çalışıyor.")
    print("Çıkmak için 'exit' yaz.")

    while True:
        user_input = await asyncio.to_thread(input, "\nSen: ")
        message = user_input.strip()

        if not message:
            continue

        if message.lower() in EXIT_COMMANDS:
            print("Uygulama kapatılıyor.")
            return

        try:
            result = await agent.run(message)
        except Exception as error:
            print(f"Ajan: {format_error(error)}")
            continue

        print(f"Ajan: {result}")


async def main() -> None:
    try:
        validate_environment()
        agent = create_agent()
    except Exception as error:
        print(format_error(error) if not isinstance(error, RuntimeError) else str(error))
        return

    await chat_loop(agent)


if __name__ == "__main__":
    asyncio.run(main())
