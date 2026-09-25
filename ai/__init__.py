from ai.jev_analyst import jev_available, jev_decide_memecoins, jev_decide_forex, jev_check_exit


async def ai_decide_memecoins(tokens, open_mints):
    if jev_available():
        return await jev_decide_memecoins(tokens, open_mints)
    from ai.analyst import ai_decide_memecoins as _openai_memecoins
    return await _openai_memecoins(tokens, open_mints)


async def ai_decide_forex(signals, open_pairs):
    if jev_available():
        return await jev_decide_forex(signals, open_pairs)
    from ai.analyst import ai_decide_forex as _openai_forex
    return await _openai_forex(signals, open_pairs)


async def ai_check_exit(position, current_price, market):
    if jev_available():
        return await jev_check_exit(position, current_price, market)
    return {"should_exit": 0.0, "should_trail": 0.0}
