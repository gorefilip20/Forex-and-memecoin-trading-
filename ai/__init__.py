from ai.jev_analyst import jev_available, jev_decide_memecoins, jev_decide_forex, jev_check_exit
from ai.analyst import ai_decide_memecoins as _openai_memecoins, ai_decide_forex as _openai_forex


async def ai_decide_memecoins(tokens, open_mints):
    if jev_available():
        return await jev_decide_memecoins(tokens, open_mints)
    return await _openai_memecoins(tokens, open_mints)


async def ai_decide_forex(signals, open_pairs):
    if jev_available():
        return await jev_decide_forex(signals, open_pairs)
    return await _openai_forex(signals, open_pairs)
