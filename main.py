
import os
import re
import asyncio
import getpass
from datetime import datetime, timedelta, timezone

import discord
import requests
from discord.ext import commands

from taf_parser import install_taf_command
from risk_command import install_risk_command
from ai_summary_command import install_ai_summary_command




# =====================================
# Discord Bot 基本設定
# =====================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

API_URL = "https://aviationweather.gov/api/data/metar"

TW_TIMEZONE = timezone(timedelta(hours=8))

AIRPORTS = {
    "桃園": "RCTP",
    "桃園機場": "RCTP",
    "松山": "RCSS",
    "松山機場": "RCSS",
    "台中": "RCMQ",
    "台中機場": "RCMQ",
    "高雄": "RCKH",
    "高雄機場": "RCKH",
    "台南": "RCNN",
    "台南機場": "RCNN",
    "花蓮": "RCYU",
    "花蓮機場": "RCYU",
    "台東": "RCFN",
    "台東機場": "RCFN",
    "澎湖": "RCQC",
    "澎湖機場": "RCQC",
    "金門": "RCBS",
    "金門機場": "RCBS",
    "南竿": "RCFG",
    "北竿": "RCMT",
    "綠島": "RCLY",
    "蘭嶼": "RCLM",
}

install_taf_command(bot, AIRPORTS)
install_risk_command(bot, AIRPORTS)
install_ai_summary_command(bot, AIRPORTS)

FLIGHT_NAMES = {
    "VFR": "目視飛行條件",
    "MVFR": "邊際目視飛行條件",
    "IFR": "儀器飛行條件",
    "LIFR": "低儀器飛行條件",
    "N/A": "資料不足",
}

CLOUD_NAMES = {
    "FEW": "少雲",
    "SCT": "疏雲",
    "BKN": "多雲",
    "OVC": "陰天",
    "VV": "垂直能見度",
}

WEATHER_NAMES = {
    "+TSRA": "強雷雨",
    "TSRA": "雷雨",
    "-TSRA": "輕雷雨",
    "+SHRA": "強陣雨",
    "SHRA": "陣雨",
    "-SHRA": "小陣雨",
    "+RA": "大雨",
    "RA": "降雨",
    "-RA": "小雨",
    "VCSH": "附近有陣雨",
    "VCTS": "附近有雷暴",
    "TS": "雷暴",
    "FG": "霧",
    "BR": "薄霧",
    "HZ": "霾",
    "DZ": "毛毛雨",
    "SN": "降雪",
    "SQ": "颮",
    "FC": "漏斗雲",
    "GR": "冰雹",
    "GS": "小冰雹",
}

# =====================================
# 時間處理
# =====================================

def convert_utc(z):
    try:
        day = int(z[:2])
        hour = int(z[2:4])
        minute = int(z[4:6])

        now = datetime.now(timezone.utc)

        candidates = []

        for offset in (-1, 0, 1):
            year = now.year
            month = now.month + offset

            if month < 1:
                month = 12
                year -= 1

            if month > 12:
                month = 1
                year += 1

            try:
                dt = datetime(
                    year, month, day,
                    hour, minute,
                    tzinfo=timezone.utc
                )
                candidates.append(dt)
            except ValueError:
                continue

        if not candidates:
            return "時間無法解析"

        dt = min(
            candidates,
            key=lambda x: abs((x - now).total_seconds())
        )

        return dt.astimezone(TW_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )

    except (ValueError, TypeError):
        return "時間無法解析"


# =====================================
# 航空氣象 API
# =====================================

def fetch_metar(icao):
    response = requests.get(
        API_URL,
        params={
            "ids": icao,
            "format": "json"
        },
        timeout=15
    )

    if response.status_code == 204:
        return None

    response.raise_for_status()

    if not response.content.strip():
        return None

    data = response.json()

    if not isinstance(data, list) or not data:
        return None

    return data[0]


# =====================================
# 天氣現象解析
# =====================================

def parse_weather(tokens):
    results = []
    codes = []

    for token in tokens:
        if token in WEATHER_NAMES:
            codes.append(token)
            results.append(
                f"{WEATHER_NAMES[token]}（{token}）"
            )

    return codes, results


# =====================================
# 雲層解析
# =====================================

def parse_clouds(tokens):
    clouds = []

    for token in tokens:
        match = re.fullmatch(
            r"(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(TCU|CB)?",
            token
        )

        if not match:
            continue

        amount = match.group(1)
        height = match.group(2)
        cloud_type = match.group(3)

        altitude = (
            int(height) * 100
            if height.isdigit()
            else None
        )

        clouds.append({
            "amount": amount,
            "altitude": altitude,
            "type": cloud_type
        })

    return clouds


def get_ceiling(clouds):
    heights = []

    for cloud in clouds:
        if cloud["amount"] in ("BKN", "OVC", "VV"):
            altitude = cloud["altitude"]

            if altitude is not None:
                heights.append(altitude)

    return min(heights) if heights else None


def format_clouds(clouds):
    if not clouds:
        return "無雲層資料"

    lines = []

    for cloud in clouds:
        amount = cloud["amount"]
        altitude = cloud["altitude"]
        cloud_type = cloud["type"]

        height_text = (
            f"{altitude} ft"
            if altitude is not None
            else "高度未知"
        )

        text = (
            f"• {height_text} "
            f"{CLOUD_NAMES.get(amount, amount)}"
        )

        if cloud_type == "CB":
            text += "（CB 積雨雲）"

        elif cloud_type == "TCU":
            text += "（TCU 塔狀積雲）"

        lines.append(text)

    return "\n".join(lines)


# =====================================
# RVR 跑道視程
# =====================================

def parse_rvr(tokens):
    results = []

    for token in tokens:
        match = re.fullmatch(
            r"R(\d{2}[LRC]?)/([MP]?\d{4})([UDN]?)",
            token
        )

        if not match:
            continue

        runway, value, trend = match.groups()

        if value.startswith("M"):
            visibility = f"低於 {value[1:]} m"

        elif value.startswith("P"):
            visibility = f"大於 {value[1:]} m"

        else:
            visibility = f"{value} m"

        trend_name = {
            "U": "上升",
            "D": "下降",
            "N": "無變化"
        }.get(trend, "")

        text = f"跑道 {runway}：{visibility}"

        if trend_name:
            text += f"（{trend_name}）"

        results.append(text)

    return results


# =====================================
# METAR 主解析器
# =====================================

def parse_metar(raw):
    raw = raw.strip()
    tokens = raw.split()

    # 分離 RMK 備註
    if "RMK" in tokens:
        index = tokens.index("RMK")
        remark_tokens = tokens[index + 1:]
        main_tokens = tokens[:index]
    else:
        remark_tokens = []
        main_tokens = tokens

    # 分離 TEMPO / BECMG 趨勢
    trend_index = next(
        (
            i for i, token in enumerate(main_tokens)
            if token in ("TEMPO", "BECMG", "NOSIG")
        ),
        len(main_tokens)
    )

    current_tokens = main_tokens[:trend_index]
    trend_tokens = main_tokens[trend_index:]

    current_raw = " ".join(current_tokens)

    result = {
        "raw": raw,
        "report_type": (
            "SPECI 特別天氣報告"
            if "SPECI" in current_tokens
            else "METAR 例行天氣報告"
        ),
        "wind_dir": None,
        "wind_speed": None,
        "gust": None,
        "variable_wind": None,
        "visibility": None,
        "temp": None,
        "dew": None,
        "qnh": None,
        "alt": None,
        "weather_codes": [],
        "weather": [],
        "clouds": [],
        "rvr": [],
        "ws": [],
        "rain_amount": None,
        "remark": " ".join(remark_tokens) or "無",
        "tempo": [],
        "utc_time": "未知",
        "tw_time": "未知",
    }

    # 時間
    time_match = re.search(
        r"\b(\d{6})Z\b",
        current_raw
    )

    if time_match:
        z = time_match.group(1)
        result["utc_time"] = z + "Z"
        result["tw_time"] = convert_utc(z)

    # 風向、風速、陣風
    wind_match = re.search(
        r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b",
        current_raw
    )

    if wind_match:
        result["wind_dir"] = wind_match.group(1)
        result["wind_speed"] = int(wind_match.group(2))

        if wind_match.group(3):
            result["gust"] = int(wind_match.group(3))

    # 風向變動
    variable = re.search(
        r"\b(\d{3})V(\d{3})\b",
        current_raw
    )

    if variable:
        result["variable_wind"] = (
            f"{variable.group(1)}° 到 "
            f"{variable.group(2)}°"
        )

    # 能見度
    if "CAVOK" in current_tokens:
        result["visibility"] = 10000

    else:
        # 取風組之後的四位數能見度
        if wind_match:
            remaining = current_raw[wind_match.end():].split()
        else:
            remaining = current_tokens

        for token in remaining:
            if re.fullmatch(r"\d{4}", token):
                result["visibility"] = int(token)
                break

        # 支援部分英里格式
        if result["visibility"] is None:
            for token in remaining:
                match = re.fullmatch(
                    r"(\d+)?(?:\s*)?(\d/\d)?SM",
                    token
                )
                if match:
                    miles = float(match.group(1) or 0)

                    if match.group(2):
                        numerator, denominator = map(
                            int, match.group(2).split("/")
                        )
                        miles += numerator / denominator

                    result["visibility"] = round(
                        miles * 1609.344
                    )
                    break

    # 溫度 / 露點
    temp_match = re.search(
        r"\b(M?\d{2})/(M?\d{2}|//)\b",
        current_raw
    )

    if temp_match:
        result["temp"] = int(
            temp_match.group(1).replace("M", "-")
        )

        if temp_match.group(2) != "//":
            result["dew"] = int(
                temp_match.group(2).replace("M", "-")
            )

    # QNH
    qnh_match = re.search(
        r"\bQ(\d{4})\b",
        current_raw
    )

    if qnh_match:
        result["qnh"] = int(qnh_match.group(1))

    # ALT：先取報告主體，再取備註
    alt_match = re.search(
        r"\bA(\d{4})\b",
        raw
    )

    if alt_match:
        result["alt"] = int(alt_match.group(1)) / 100

    elif result["qnh"] is not None:
        result["alt"] = round(
            result["qnh"] / 33.8639, 2
        )

    # 天氣現象
    codes, weather = parse_weather(current_tokens)

    result["weather_codes"] = codes
    result["weather"] = weather

    # 雲層
    result["clouds"] = parse_clouds(current_tokens)

    # RVR
    result["rvr"] = parse_rvr(current_tokens)

    # 風切
    ws_match = re.findall(
        r"\bWS\s+(R\d{2}[LRC]?|ALL RWY)\b",
        current_raw
    )

    result["ws"] = [
        f"風切警告：{runway}"
        for runway in ws_match
    ]

    # 雨量（備註）
    rain_match = re.search(
        r"\bRA\s+AMT\s+([\d.]+)MM\b",
        raw
    )

    if rain_match:
        result["rain_amount"] = (
            f"{rain_match.group(1)} mm"
        )

    # TEMPO / BECMG / NOSIG
    if trend_tokens:
        trend_text = " ".join(trend_tokens)

        for token in trend_tokens:
            if token in ("TEMPO", "BECMG", "NOSIG"):
                result["tempo"].append(token)

        trend_vis = re.search(
            r"\b(\d{4})\b",
            trend_text
        )

        if trend_vis:
            result["tempo"].append(
                f"趨勢能見度：{int(trend_vis.group(1))} m"
            )

        _, trend_weather = parse_weather(trend_tokens)
        result["tempo"].extend(trend_weather)

    return result


# =====================================
# 飛行條件判斷
# =====================================

def flight_category(data):
    visibility = data["visibility"]
    ceiling = get_ceiling(data["clouds"])

    if visibility is None and ceiling is None:
        return "N/A"

    # 使用美國航空氣象常用分類
    # 能見度以 statute mile 為基準
    # 任一條件達到較差等級即採該等級

    if (
        (visibility is not None and visibility < 1609.344)
        or (ceiling is not None and ceiling < 500)
    ):
        return "LIFR"

    if (
        (visibility is not None and visibility < 4828.032)
        or (ceiling is not None and ceiling < 1000)
    ):
        return "IFR"

    if (
        (visibility is not None and visibility <= 8046.72)
        or (ceiling is not None and ceiling <= 3000)
    ):
        return "MVFR"

    return "VFR"


# =====================================
# 飛行風險分析
# =====================================

def analyze_risks(data, category):
    risks = []

    visibility = data["visibility"]
    ceiling = get_ceiling(data["clouds"])

    speed = data["wind_speed"]
    gust = data["gust"]

    weather = data["weather_codes"]

    if category == "LIFR":
        risks.append("低儀器飛行條件")

    elif category == "IFR":
        risks.append("儀器飛行條件")

    if speed is not None and speed >= 25:
        risks.append("強風")

    if gust is not None and gust >= 20:
        risks.append("強陣風")

    if visibility is not None and visibility < 5000:
        risks.append("低能見度")

    if ceiling is not None and ceiling <= 1000:
        risks.append("低雲")

    if any("TS" in code for code in weather):
        risks.append("雷暴")

    if any(
        code.startswith("+") and "RA" in code
        for code in weather
    ):
        risks.append("強降雨")

    elif any("RA" in code for code in weather):
        risks.append("降雨")

    if any(
        cloud["type"] == "CB"
        for cloud in data["clouds"]
    ):
        risks.append("CB 積雨雲活動")

    if any(
        cloud["type"] == "TCU"
        for cloud in data["clouds"]
    ):
        risks.append("TCU 塔狀積雲活動")

    if data["ws"]:
        risks.append("風切警告")

    if data["rain_amount"]:
        risks.append("有累積降雨紀錄")

    return risks


# =====================================
# 中文摘要
# =====================================

def build_summary(icao, category, risks):
    summary = (
        f"{icao} 目前為 {category}"
        f"（{FLIGHT_NAMES.get(category, '未知')}）。"
    )

    if risks:
        summary += (
            "\n需注意：" + "、".join(risks) + "。"
        )
    else:
        summary += "\n目前未偵測到預設的主要風險項目。"

    return summary


# =====================================
# Discord 顯示工具
# =====================================

def safe_text(value, limit=1024):
    text = str(value).strip() if value is not None else ""

    if not text:
        text = "無資料"

    if len(text) > limit:
        text = text[:limit - 3] + "..."

    return text


def add_field(embed, name, value):
    embed.add_field(
        name=name,
        value=safe_text(value),
        inline=False
    )


# =====================================
# Discord 啟動事件
# =====================================

@bot.event
async def on_ready():
    print("=" * 45)
    print(f"Discord Bot 已登入：{bot.user}")
    print("航空氣象查詢系統已啟動")
    print("使用指令：!metar 高雄")
    print("=" * 45)


# =====================================
# METAR 查詢指令
# =====================================

@bot.command()
async def metar(ctx, *, airport=None):

    if not airport:
        await ctx.send(
            "✈ 請輸入機場名稱或 ICAO\n"
            "例如：`!metar 高雄` 或 `!metar RCKH`"
        )
        return

    airport = airport.strip()

    if len(airport) == 4 and airport.isalpha():
        icao = airport.upper()
    else:
        icao = AIRPORTS.get(airport)

    if not icao:
        await ctx.send("❌ 找不到機場，請確認名稱。")
        return

    async with ctx.typing():
        try:
            # 避免同步網路請求阻塞 Discord
            metar_data = await asyncio.to_thread(
                fetch_metar, icao
            )

        except requests.exceptions.Timeout:
            await ctx.send(
                "⚠ 航空氣象 API 連線逾時，請稍後重試。"
            )
            return

        except requests.exceptions.RequestException as e:
            await ctx.send(
                f"❌ API 連線失敗：{safe_text(e, 300)}"
            )
            return

        except ValueError:
            await ctx.send(
                "❌ API 回傳資料格式錯誤。"
            )
            return

        if not metar_data:
            await ctx.send(
                f"⚠ {icao} 目前沒有 METAR 資料。"
            )
            return

        raw = metar_data.get("rawOb")

        if not raw:
            await ctx.send(
                "⚠ API 沒有提供原始 METAR。"
            )
            return

        try:
            parsed = parse_metar(raw)
            category = flight_category(parsed)
            risks = analyze_risks(parsed, category)

        except Exception as e:
            print("METAR 解析錯誤：", repr(e))
            await ctx.send(
                "❌ METAR 解析發生錯誤，請檢查終端機訊息。"
            )
            return

    # =================================
    # 基本氣象文字
    # =================================

    wind_dir = parsed["wind_dir"]

    if wind_dir == "VRB":
        wind_text = "風向不定"

    elif wind_dir is not None:
        wind_text = f"{wind_dir}°"

    else:
        wind_text = "未知"

    speed = parsed["wind_speed"]

    speed_text = (
        f"{speed} KT"
        if speed is not None
        else "未知"
    )

    weather_lines = [
        f"📌 報告類型：{parsed['report_type']}",
        f"🌬 風向：{wind_text}",
        f"💨 風速：{speed_text}",
    ]

    if parsed["gust"] is not None:
        weather_lines.append(
            f"🌪 陣風：{parsed['gust']} KT"
        )

    if parsed["variable_wind"]:
        weather_lines.append(
            f"🔄 風向變動：{parsed['variable_wind']}"
        )

    visibility = parsed["visibility"]

    visibility_text = (
        f"{visibility} m"
        if visibility is not None
        else "未知"
    )

    weather_lines.append(
        f"👁 能見度：{visibility_text}"
    )

    temp = parsed["temp"]
    dew = parsed["dew"]

    temp_text = (
        f"{temp}°C"
        if temp is not None
        else "未知"
    )

    dew_text = (
        f"{dew}°C"
        if dew is not None
        else "未知"
    )

    weather_lines.append(
        f"🌡 溫度：{temp_text} / 露點：{dew_text}"
    )

    qnh = parsed["qnh"]

    weather_lines.append(
        f"📈 QNH：{qnh} hPa"
        if qnh is not None
        else "📈 QNH：未知"
    )

    alt = parsed["alt"]

    weather_lines.append(
        f"📏 ALT：{alt:.2f} inHg"
        if alt is not None
        else "📏 ALT：未知"
    )

    # =================================
    # Discord Embed
    # =================================

    color = {
        "VFR": 0x2ECC71,
        "MVFR": 0x3498DB,
        "IFR": 0xE74C3C,
        "LIFR": 0x9B59B6,
    }.get(category, 0x95A5A6)

    embed = discord.Embed(
        title=f"✈ {icao} 航空氣象解析",
        description=safe_text(raw, 4000),
        color=color
    )

    add_field(
        embed,
        "🕒 時間",
        (
            f"UTC：{parsed['utc_time']}\n"
            f"台灣時間：{parsed['tw_time']}"
        )
    )

    add_field(
        embed,
        "🌤 氣象解析",
        "\n".join(weather_lines)
    )

    add_field(
        embed,
        "🌧 目前天氣現象",
        "\n".join(parsed["weather"])
        if parsed["weather"]
        else "無明顯天氣現象"
    )

    add_field(
        embed,
        "⏱ 天氣趨勢",
        "\n".join(parsed["tempo"])
        if parsed["tempo"]
        else "無顯著趨勢資料"
    )

    add_field(
        embed,
        "🛬 跑道視程 RVR",
        "\n".join(parsed["rvr"])
        if parsed["rvr"]
        else "無跑道視程資料"
    )

    add_field(
        embed,
        "⚠ 風切 WS",
        "\n".join(parsed["ws"])
        if parsed["ws"]
        else "無風切資料"
    )

    add_field(
        embed,
        "🌧 雨量",
        parsed["rain_amount"] or "無雨量資料"
    )

    add_field(
        embed,
        "☁ 雲層",
        format_clouds(parsed["clouds"])
    )

    ceiling = get_ceiling(parsed["clouds"])

    add_field(
        embed,
        "☁ Ceiling 雲冪",
        f"{ceiling} ft"
        if ceiling is not None
        else "未觀測到可判定的雲冪"
    )

    add_field(
        embed,
        "✈ 飛行條件",
        (
            f"**{category}**\n"
            f"{FLIGHT_NAMES.get(category, '未知')}"
        )
    )

    add_field(
        embed,
        "⚠ 飛行風險",
        "\n".join(f"• {risk}" for risk in risks)
        if risks
        else "✅ 無預設主要風險"
    )

    add_field(
        embed,
        "📝 備註 RMK",
        parsed["remark"]
    )

    add_field(
        embed,
        "🤖 智慧氣象摘要",
        build_summary(icao, category, risks)
    )

    embed.set_footer(
        text=(
            "航空氣象研究與教學輔助系統｜"
            "不得取代正式飛航決策"
        )
    )

    await ctx.send(embed=embed)


# =====================================
# 機場清單指令
# =====================================

@bot.command()
async def airports(ctx):
    lines = []

    for name, code in AIRPORTS.items():
        if "機場" not in name:
            lines.append(f"{name}：{code}")

    await ctx.send(
        "✈ 支援機場\n" + "\n".join(lines)
    )


# =====================================
# 指令錯誤處理
# =====================================

@metar.error
async def metar_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("請輸入機場，例如：!metar 高雄")
    else:
        print("指令錯誤：", repr(error))
        await ctx.send("❌ 指令執行失敗，請查看終端機。")


# =====================================
# 啟動 Bot
# =====================================

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")

    if not token:
        print("❌ 找不到 DISCORD_TOKEN 環境變數")
        print("請到 PyCharm Run Configuration 設定 Token")
    else:
        print("🚀 正在啟動 Discord Bot...")
        bot.run(token)
