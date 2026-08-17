import os
import io
import requests

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from datetime import datetime


MARKETDATA_TOKEN = os.environ.get("MARKETDATA_TOKEN")


def get_chart_data(symbol, timeframe="60", countback=100):
    url = (
        f"https://api.marketdata.app/"
        f"v1/stocks/candles/{timeframe}/{symbol}/"
    )

    headers = {
        "Authorization": f"Bearer {MARKETDATA_TOKEN}"
    }

    params = {
        "countback": countback
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=20
    )

    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        raise ValueError("تعذر الحصول على بيانات الشارت.")

    return data


def create_chart(symbol, timeframe="60"):
    data = get_chart_data(
        symbol,
        timeframe=timeframe,
        countback=100
    )

    timestamps = data.get("t", [])
    opens = data.get("o", [])
    highs = data.get("h", [])
    lows = data.get("l", [])
    closes = data.get("c", [])

    if not timestamps or len(closes) < 10:
        raise ValueError("بيانات الشارت غير كافية.")

    dates = [
        datetime.fromtimestamp(ts)
        for ts in timestamps
    ]

    fig, ax = plt.subplots(
        figsize=(11, 6)
    )

    for i in range(len(dates)):
        open_price = float(opens[i])
        high_price = float(highs[i])
        low_price = float(lows[i])
        close_price = float(closes[i])

        if close_price >= open_price:
            candle_color = "#22c55e"
        else:
            candle_color = "#ef4444"

        ax.vlines(
            dates[i],
            low_price,
            high_price,
            color=candle_color,
            linewidth=1
        )

        body_bottom = min(
            open_price,
            close_price
        )

        body_height = abs(
            close_price - open_price
        )

        if body_height == 0:
            body_height = 0.01

        ax.bar(
            dates[i],
            body_height,
            bottom=body_bottom,
            width=0.02,
            color=candle_color,
            align="center"
        )

    last_price = float(closes[-1])

    ax.axhline(
        last_price,
        linestyle="--",
        linewidth=1,
        alpha=0.6
    )

    ax.set_title(
        f"{symbol.upper()} | {timeframe}m | ${last_price:.2f}",
        fontsize=16,
        fontweight="bold"
    )

    ax.set_ylabel("Price")

    ax.grid(
        True,
        alpha=0.15
    )

    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%m/%d")
    )

    fig.autofmt_xdate()

    plt.tight_layout()

    image = io.BytesIO()

    plt.savefig(
        image,
        format="png",
        dpi=160,
        bbox_inches="tight"
    )

    plt.close(fig)

    image.seek(0)

    image.name = f"{symbol.upper()}_chart.png"

    return image
