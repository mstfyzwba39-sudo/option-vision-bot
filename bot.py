def add_pending_watch(
    chat_id,
    symbol,
    contract,
    trend
):
    # لازم العقد يكون موافق للاتجاه
    if trend["bias"] == "NEUTRAL":
        return False

    if contract["side"] != trend["bias"]:
        return False

    # لازم يحقق شروط أفضل الفرص
    if contract["score"] < MIN_TOP_SCORE:
        return False

    if contract["uoa_score"] < MIN_TOP_UOA:
        return False

    if contract["ask"] > MAX_OPTION_ASK:
        return False

    key = watch_key(
        chat_id,
        symbol
    )

    PENDING_WATCHES[key] = {
        "chat_id": chat_id,
        "symbol": symbol,
        "side": contract["side"],
        "strike": contract["strike"],
        "option_symbol": contract["option_symbol"],
        "original_ask": contract["ask"],
        "created_at": time.time(),
    }

    print(
        "WATCH ADDED:",
        symbol,
        contract["side"],
        contract["strike"]
    )

    return True
