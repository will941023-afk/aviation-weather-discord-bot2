"""Discord !risk: METAR observations and TAF forecast hazard signals.

Educational demonstration only; not for operational flight decisions.
Requires discord.py and requests. Independent of other project modules.
"""

import asyncio
import re

import discord
import requests

API_ROOT = "https://aviationweather.gov/api/data"
WIND_RE = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT$")
CLOUD_RE = re.compile(r"^(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?$")
CHANGE_RE = re.compile(r"^(?:FM\d{6}|TEMPO|BECMG|PROB30|PROB40)$")
WX_RE = re.compile(r"^[+-]?(?:VC)?(?:TS|SH|FZ)?(?:RA|DZ|SN|GR|GS|FG|BR|HZ|TS)+$")


def _fetch_report(kind, icao):
    if kind not in ("metar", "taf"):
        raise ValueError("不支援的報告類型")
    response = requests.get(
        f"{API_ROOT}/{kind}",
        params={"ids": icao, "format": "json"},
        headers={"User-Agent": "AviationWeatherSchoolProject/1.0"},
        timeout=15,
    )
    if response.status_code == 204 or not response.content.strip():
        return None
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else None
    return data if isinstance(data, dict) else None


def _raw_text(record, kind):
    if not isinstance(record, dict):
        return ""
    keys = ("rawOb", "rawMETAR", "rawMetar", "raw") if kind == "metar" else (
        "rawTAF", "rawTaf", "raw", "rawOb"
    )
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _taf_groups(raw):
    tokens = raw.replace("\n", " ").replace("=", " ").split()
    groups = [{"label": "基本預報", "tokens": []}]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if CHANGE_RE.fullmatch(token):
            if token.startswith("FM"):
                label = f"FM {token[2:4]}日 {token[4:6]}:{token[6:8]} UTC 起"
            else:
                label = {
                    "TEMPO": "TEMPO 暫時變化",
                    "BECMG": "BECMG 逐漸轉變",
                    "PROB30": "PROB30 機率預報",
                    "PROB40": "PROB40 機率預報",
                }[token]
            if token.startswith("PROB") and i + 1 < len(tokens) and tokens[i + 1] == "TEMPO":
                i += 1
                label += " / TEMPO"
            if i + 1 < len(tokens) and re.fullmatch(r"\d{4}/\d{4}", tokens[i + 1]):
                i += 1
                label += f"（{tokens[i]} UTC）"
            groups.append({"label": label, "tokens": []})
        else:
            groups[-1]["tokens"].append(token)
        i += 1
    return groups


def _signals(tokens):
    """Only flag conditions explicitly present in the given tokens."""
    risks = []
    for token in tokens:
        wind = WIND_RE.fullmatch(token)
        if wind:
            speed = int(wind.group(2))
            gust = int(wind.group(3)) if wind.group(3) else 0
            if speed >= 25:
                risks.append(f"強風（{speed} kt）")
            if gust >= 20:
                risks.append(f"強陣風（{gust} kt）")
            continue

        cloud = CLOUD_RE.fullmatch(token)
        if cloud:
            amount, height, cloud_type = cloud.groups()
            feet = int(height) * 100
            if amount in ("BKN", "OVC", "VV") and feet <= 1000:
                risks.append(f"低雲底（{feet} ft）")
            if cloud_type == "CB":
                risks.append("CB 積雨雲")
            if cloud_type == "TCU":
                risks.append("TCU 塔狀積雲")
            continue

        # 4-digit visibility must not be confused with date/time or validity tokens.
        if re.fullmatch(r"\d{4}", token):
            visibility = int(token)
            if visibility < 5000:
                risks.append(f"低能見度（{visibility} m）")
            continue

        if WX_RE.fullmatch(token):
            if "TS" in token:
                risks.append("雷暴")
            if "RA" in token:
                risks.append("強降雨" if token.startswith("+") else "降雨")
            if "FG" in token:
                risks.append("霧")
            if "SN" in token:
                risks.append("降雪")

    return list(dict.fromkeys(risks))


def _metar_tokens(raw):
    # Ignore remark and trend sections: these are not current observations.
    tokens = raw.replace("=", " ").split()
    for i, token in enumerate(tokens):
        if token in ("RMK", "NOSIG", "TEMPO", "BECMG", "TREND"):
            return tokens[:i]
    return tokens


def _forecast_sections(raw):
    sections = []
    for group in _taf_groups(raw):
        risks = _signals(group["tokens"])
        if risks:
            sections.append((group["label"], risks))
    return sections


def _short(text, limit=1000):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _conditions(tokens):
    """Return (visibility metres, ceiling feet) for explicitly stated conditions."""
    visibility = None
    ceilings = []
    for token in tokens:
        if token == "CAVOK":
            visibility = 10000
        elif re.fullmatch(r"\d{4}", token):
            visibility = int(token)
        cloud = CLOUD_RE.fullmatch(token)
        if cloud and cloud.group(1) in ("BKN", "OVC", "VV"):
            ceilings.append(int(cloud.group(2)) * 100)
    return visibility, min(ceilings) if ceilings else None


def _category(tokens):
    """US-style flight category thresholds; unknown when key information is absent."""
    vis, ceiling = _conditions(tokens)
    if vis is None and ceiling is None:
        return "資料不足"
    # The more restrictive of visibility and ceiling determines the category.
    # When only one is known, report a partial assessment rather than a definitive class.
    ranks = []
    if vis is not None:
        ranks.append(0 if vis < 1609.344 else 1 if vis < 4828.032 else 2 if vis <= 8046.72 else 3)
    if ceiling is not None:
        ranks.append(0 if ceiling < 500 else 1 if ceiling < 1000 else 2 if ceiling <= 3000 else 3)
    names = ["LIFR", "IFR", "MVFR", "VFR"]
    result = names[min(ranks)]
    cloud_reported = any(CLOUD_RE.fullmatch(t) for t in tokens)
    clear_reported = any(t in ("CAVOK", "NSC", "SKC", "CLR") for t in tokens)
    if vis is None or (ceiling is None and not cloud_reported and not clear_reported):
        return result + "（部分資料判斷）"
    return result


def _wind_from_tokens(tokens):
    for token in tokens:
        match = WIND_RE.fullmatch(token)
        if match:
            direction = None if match.group(1) == "VRB" else int(match.group(1))
            return direction, int(match.group(2)), int(match.group(3) or 0)
    return None


def _wind_changes(metar_tokens, taf_raw):
    current = _wind_from_tokens(metar_tokens)
    groups = _taf_groups(taf_raw)
    forecast = [(g["label"], _wind_from_tokens(g["tokens"])) for g in groups]
    forecast = [(label, wind) for label, wind in forecast if wind is not None]
    if not forecast:
        return "TAF 未明示風向／風速變化。"
    lines = []
    if current:
        direction, speed, _ = current
        lines.append(f"METAR 現況：{'VRB' if direction is None else str(direction).zfill(3) + '°'}／{speed} kt")
    for label, (direction, speed, _) in forecast[:6]:
        lines.append(f"{label}：{'VRB' if direction is None else str(direction).zfill(3) + '°'}／{speed} kt")
    directions = [wind[0] for _, wind in forecast if wind[0] is not None]
    if len(set(directions)) >= 2:
        lines.append("⚠️ 預報包含不同風向；請留意可能的風向轉變。")
    lines.append("註：TEMPO 為暫時變化，BECMG 為逐漸轉變；各段不一定依序立即生效。")
    return "\n".join(lines)


def install_risk_command(bot, airports):
    """Register !risk exactly once on the existing discord.py commands.Bot."""
    if bot.get_command("risk") is not None:
        return

    @bot.command(name="risk")
    async def risk(ctx, *, airport=None):
        if not airport or not airport.strip():
            await ctx.send("請輸入機場，例如：`!risk 高雄` 或 `!risk RCKH`")
            return

        name = airport.strip()
        icao = airports.get(name, name.upper())
        if not isinstance(icao, str) or not re.fullmatch(r"[A-Z]{4}", icao):
            await ctx.send("找不到機場代碼，請輸入中文機場名稱或四碼 ICAO，例如 `!risk RCKH`。")
            return

        async with ctx.typing():
            results = await asyncio.gather(
                asyncio.to_thread(_fetch_report, "metar", icao),
                asyncio.to_thread(_fetch_report, "taf", icao),
                return_exceptions=True,
            )

        metar_result, taf_result = results
        embed = discord.Embed(
            title=f"✈️ {icao} 航空氣象風險分析",
            description="METAR 為目前觀測；TAF 為未來預報，兩者分開判讀。",
            color=discord.Color.orange(),
        )

        if isinstance(metar_result, Exception):
            embed.add_field(name="📍 METAR 目前觀測", value="資料取得失敗，請稍後重試。", inline=False)
        else:
            raw_metar = _raw_text(metar_result, "metar")
            if raw_metar:
                risks = _signals(_metar_tokens(raw_metar))
                summary = "\n".join(f"• {item}" for item in risks) if risks else "未辨識到設定的風險訊號（不代表無風險）。"
                category = _category(_metar_tokens(raw_metar))
                embed.add_field(name="📍 METAR 目前觀測", value=_short(f"飛行條件：**{category}**\n{summary}"), inline=False)
                embed.add_field(name="METAR 原文", value=f"```{_short(raw_metar, 900)}```", inline=False)
            else:
                embed.add_field(name="📍 METAR 目前觀測", value="查無 METAR 資料。", inline=False)

        if isinstance(taf_result, Exception):
            embed.add_field(name="📅 TAF 未來預報", value="資料取得失敗，請稍後重試。", inline=False)
        else:
            raw_taf = _raw_text(taf_result, "taf")
            if raw_taf:
                sections = _forecast_sections(raw_taf)
                if sections:
                    lines = [f"**{label}**：{'、'.join(risks)}" for label, risks in sections]
                    forecast_text = "\n".join(lines)
                else:
                    forecast_text = "未辨識到明確列出的風險訊號（不代表無風險）。"
                embed.add_field(name="📅 TAF 預報風險訊號", value=_short(forecast_text), inline=False)
                if not isinstance(metar_result, Exception):
                    raw_for_wind = _raw_text(metar_result, "metar")
                else:
                    raw_for_wind = ""
                embed.add_field(
                    name="🌬️ METAR／TAF 風向變化比較",
                    value=_short(_wind_changes(_metar_tokens(raw_for_wind), raw_taf)),
                    inline=False,
                )
            else:
                embed.add_field(name="📅 TAF 未來預報", value="查無 TAF 資料。", inline=False)

        embed.set_footer(text="專題教學用途｜非完整航務決策或飛航安全評估")
        await ctx.send(embed=embed)
