"""TAF basic parser and Discord command installer. Educational, not operational.

All displayed forecast periods are converted from UTC to Taiwan time (UTC+8).
The original TAF report is preserved unchanged.
"""
import re
import asyncio
from datetime import datetime, timezone, timedelta
import calendar
import requests
import discord

TAF_URL = 'https://aviationweather.gov/api/data/taf'
TW_TZ = timezone(timedelta(hours=8))
WEATHER = {'TSRA':'雷雨','+TSRA':'強雷雨','-TSRA':'輕雷雨','RA':'降雨','+RA':'強降雨','-RA':'小雨','SHRA':'陣雨','+SHRA':'強陣雨','-SHRA':'小陣雨','FG':'霧','BR':'薄霧','HZ':'霾','SN':'雪','VCTS':'附近有雷暴','NSW':'無顯著天氣'}
CLOUDS = {'FEW':'少雲','SCT':'疏雲','BKN':'多雲','OVC':'陰天','VV':'垂直能見度'}


def fetch_taf(icao):
    r = requests.get(TAF_URL, params={'ids': icao, 'format': 'json'}, timeout=15)
    if r.status_code == 204 or not r.content.strip():
        return None
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None


def _month_shift(year, month, offset):
    index = year * 12 + month - 1 + offset
    return index // 12, index % 12 + 1


def _resolve_utc(day, hour, minute, anchor):
    """Find closest plausible UTC calendar date to the TAF issue date."""
    candidates = []
    for offset in (-1, 0, 1):
        year, month = _month_shift(anchor.year, anchor.month, offset)
        if 1 <= day <= calendar.monthrange(year, month)[1]:
            # 24:00 denotes midnight of the following day in TAF validity periods.
            if hour == 24 and minute == 0:
                dt = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(days=1)
            elif 0 <= hour <= 23 and 0 <= minute <= 59:
                dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            else:
                return None
            candidates.append(dt)
    return min(candidates, key=lambda dt: abs((dt - anchor).total_seconds())) if candidates else None


def _taf_issue_time(tokens, reference=None):
    """Use TAF issuance DDHHMMZ as the month/year anchor."""
    now = reference or datetime.now(timezone.utc)
    for token in tokens[:8]:
        m = re.fullmatch(r'(\d{2})(\d{2})(\d{2})Z', token)
        if m:
            resolved = _resolve_utc(*map(int, m.groups()), now)
            if resolved:
                return resolved
    return now


def _period_tw(window, anchor):
    match = re.fullmatch(r'(\d{2})(\d{2})/(\d{2})(\d{2})', window)
    if not match:
        return window
    d1, h1, d2, h2 = map(int, match.groups())
    start = _resolve_utc(d1, h1, 0, anchor)
    if not start:
        return window
    end = _resolve_utc(d2, h2, 0, start + timedelta(hours=12))
    if not end:
        return window
    if end <= start:
        # Validity window may cross the end of a month.
        year, month = _month_shift(start.year, start.month, 1)
        if d2 <= calendar.monthrange(year, month)[1]:
            end = datetime(year, month, d2, tzinfo=timezone.utc) + timedelta(hours=h2)
    if end <= start:
        return window
    a, b = start.astimezone(TW_TZ), end.astimezone(TW_TZ)
    return f'{a:%m/%d %H:%M}～{b:%m/%d %H:%M}（台灣時間）'


def _fm_tw(token, anchor):
    match = re.fullmatch(r'FM(\d{2})(\d{2})(\d{2})', token)
    if not match:
        return token
    dt = _resolve_utc(*map(int, match.groups()), anchor)
    return f'FM 從 {dt.astimezone(TW_TZ):%m/%d %H:%M}（台灣時間）起' if dt else token


def parse_conditions(tokens):
    lines = []
    for token in tokens:
        w = re.fullmatch(r'(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT', token)
        if w:
            direction = '不定' if w[1] == 'VRB' else w[1] + '°'
            lines.append(f'🌬 風向：{direction}，風速：{w[2]} KT' + (f'，陣風：{w[3]} KT' if w[3] else ''))
            continue
        if token == 'CAVOK':
            lines.append('👁 CAVOK（能見度至少10公里、無重要天氣及重要低雲）')
        elif re.fullmatch(r'\d{4}', token):
            lines.append(f'👁 能見度：{int(token)} m' if token != '9999' else '👁 能見度：10 km 或以上')
        elif token in WEATHER:
            lines.append('🌧 ' + WEATHER[token] + '（' + token + '）')
        else:
            c = re.fullmatch(r'(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?', token)
            if c:
                lines.append(f'☁ {CLOUDS[c[1]]} {int(c[2])*100} ft' + (f'，{c[3]}' if c[3] else ''))
    return lines


def parse_taf(raw, reference=None):
    tokens = raw.replace('\n', ' ').replace('=', ' ').split()
    if not tokens:
        return []
    anchor = _taf_issue_time(tokens, reference)
    groups = []
    current = {'title': '基本預報', 'tokens': []}
    groups.append(current)
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if re.fullmatch(r'FM\d{6}', t):
            current = {'title': _fm_tw(t, anchor), 'tokens': []}
            groups.append(current)
        elif t in ('TEMPO', 'BECMG'):
            title = {'TEMPO': '暫時變化', 'BECMG': '逐漸轉變'}[t]
            if i + 1 < len(tokens) and re.fullmatch(r'\d{4}/\d{4}', tokens[i+1]):
                title += f'（{_period_tw(tokens[i+1], anchor)}）'
                i += 1
            current = {'title': title, 'tokens': []}
            groups.append(current)
        elif re.fullmatch(r'PROB(30|40)', t):
            title = f'機率 {t[4:]}%'
            if i + 1 < len(tokens) and tokens[i+1] == 'TEMPO':
                title += '，暫時變化'
                i += 1
            if i + 1 < len(tokens) and re.fullmatch(r'\d{4}/\d{4}', tokens[i+1]):
                title += f'（{_period_tw(tokens[i+1], anchor)}）'
                i += 1
            current = {'title': title, 'tokens': []}
            groups.append(current)
        else:
            current['tokens'].append(t)
        i += 1

    window = next((t for t in tokens[:8] if re.fullmatch(r'\d{4}/\d{4}', t)), None)
    output = []
    if window:
        output.append(f'📅 預報有效期：{_period_tw(window, anchor)}')
    if 'AMD' in tokens[:5]:
        output.append('📝 修正預報 AMD')
    if 'COR' in tokens[:5]:
        output.append('📝 更正預報 COR')
    for group in groups:
        details = parse_conditions(group['tokens'])
        if details:
            output.append('**' + group['title'] + '**\n' + '\n'.join(details))
    return output


def install_taf_command(bot, airports):
    @bot.command(name='taf')
    async def taf(ctx, *, airport=None):
        if not airport:
            await ctx.send('請輸入機場，例如：`!taf 高雄` 或 `!taf RCKH`')
            return
        name = airport.strip()
        icao = name.upper() if re.fullmatch(r'[A-Za-z]{4}', name) else airports.get(name)
        if not icao:
            await ctx.send('❌ 找不到機場，請確認名稱或 ICAO。')
            return
        async with ctx.typing():
            try:
                data = await asyncio.to_thread(fetch_taf, icao)
            except requests.RequestException:
                await ctx.send('❌ TAF API 連線失敗或逾時，請稍後重試。')
                return
            except ValueError:
                await ctx.send('❌ TAF API 回傳格式錯誤。')
                return
        raw = data.get('rawTAF') if data else None
        if not raw:
            await ctx.send(f'⚠ {icao} 目前沒有可用的 TAF 預報。')
            return
        embed = discord.Embed(title=f'✈ {icao} TAF 航空氣象預報', description=raw[:4000], color=0x3498db)
        details = parse_taf(raw)
        if not details:
            details = ['無法解析此份 TAF 的詳細欄位，請查看原始報文。']
        for i, part in enumerate(details[:20]):
            embed.add_field(name='預報有效期' if i == 0 and part.startswith('📅') else f'預報段落 {i+1}', value=part[:1024], inline=False)
        embed.set_footer(text='TAF 時間已轉換為台灣時間（UTC+8）｜僅供研究教學，不可作為飛航決策依據')
        await ctx.send(embed=embed)
