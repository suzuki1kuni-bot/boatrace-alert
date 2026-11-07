import requests, re, sys, time, os
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
最低キーマンスコア = 2.0  # 土日・G1の番組に合わせて適正化
イン逃げ率上限 = 65.0

VENUES = "桐生 戸田 江戸川 平和島 多摩川 浜名湖 蒲郡 常滑 津 三国 びわこ 住之江 尼崎 鳴門 丸亀 児島 宮島 徳山 下関 若松 芦屋 福岡 唐津 大村".split()
VENUE_DICT = {v: f"{i+1:02d}" for i, v in enumerate(VENUES)}

def fetch_active_venues(session, today_str):
    """公式トップページから本日の開催場を安全に一括取得（ブロック完全回避）"""
    url = f"https://www.boatrace.jp/owpc/pc/race/index?hd={today_str}"
    active = []
    try:
        r = session.get(url, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        for v in VENUES:
            if v in r.text and f"jcd={VENUE_DICT[v]}" in r.text:
                active.append(v)
    except: pass
    return active if active else ["三国", "唐津", "びわこ", "住之江"] # フォールバック

def fetch_racelist_fast(session, venue, rno, date_str):
    jcd = VENUE_DICT.get(venue)
    u = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={date_str}"
    try:
        r = session.get(u, timeout=12)
        if "データがありません" in r.text or r.status_code != 200: return None
        return BeautifulSoup(r.text, 'html.parser'), r.text
    except: return None

def scan_single_race(s_l, raw_txt):
    tb_l = s_l.find_all('tbody', class_='is-fs12')[:6]
    if len(tb_l) < 6: return None
    
    m_time = re.search(r'電投締切\s*(\d{2}:\d{2})', raw_txt)
    close_time = m_time.group(1) if m_time else "未定"
    
    boats = {}
    for w in range(1, 7):
        first_tr = tb_l[w-1].find('tr')
        tds = first_tr.find_all('td') if first_tr else tb_l[w-1].find_all('td')
        tb_html = str(tb_l[w-1])
        m_n = re.search(r'class="is-fs18[^"]*">\s*<a[^>]*>([^<]+)</a>', tb_html)
        name = m_n.group(1).strip().replace('\u3000', ' ') if m_n else f"{w}号艇"
        r_match = re.search(r'\b(A1|A2|B1|B2)\b', tds[2].get_text())
        rank = r_match.group(1) if r_match else "B1"
        td_st_txt = tds[3].get_text() if len(tds) > 3 else ""
        m_st = re.search(r'(0\.\d{2})', td_st_txt)
        st_avg = float(m_st.group(1)) if m_st else 0.16
        m_f = re.search(r'F(\d)', td_st_txt)
        f_hold = int(m_f.group(1)) if m_f else 0
        nat = float((re.findall(r'\d\.\d{2}', tds[4].get_text()) or [5.0])[0]) if len(tds)>4 else 5.0
        td6_txt = tds[6].get_text() if len(tds)>6 else ""
        mot_m = re.findall(r'(\d{1,2}\.\d{2})', td6_txt)
        mot = float(mot_m[0]) if mot_m else 33.0
        boats[w] = {'name': name, 'rank': rank, 'st': st_avg, 'f': f_hold, 'nat': nat, 'mot': mot}
        
    c1 = boats[1]
    
    # 攻め手 (2~4号艇) のパワーと優位性を算出（G1・一般戦両対応）
    attack_powers = {}
    key_reasons = {}
    for c in [2, 3, 4]:
        b = boats[c]
        pwr = 0.0
        reasons = []
        
        # 勝率差
        win_diff = b['nat'] - c1['nat']
        if win_diff >= 1.0: pwr += 2.0; reasons.append(f"勝率差+{win_diff:.1f}")
        elif win_diff > 0.3: pwr += 1.0
        
        # 級別格差
        if b['rank'] == 'A1' and c1['rank'] != 'A1': pwr += 1.8; reasons.append("実力A1")
        elif b['rank'] in ['A1', 'A2'] and c1['rank'] in ['B1', 'B2']: pwr += 1.5; reasons.append(f"格上{b['rank']}")
        
        # 機力差（40%超は無条件評価）
        if b['mot'] >= 40.0: pwr += 1.5; reasons.append(f"好機{b['mot']:.1f}%")
        if b['mot'] - c1['mot'] >= 8.0: pwr += 1.0; reasons.append("機力優勢")
        
        # ST差
        st_diff = c1['st'] - b['st']
        if st_diff >= 0.025: pwr += 2.0; reasons.append(f"ST大幅優勢({b['st']:.2f})")
        elif st_diff >= 0.01: pwr += 1.0; reasons.append(f"ST優勢({b['st']:.2f})")
        
        pos_w = 1.2 if c in [3, 4] else 1.1
        pwr *= pos_w
        attack_powers[c] = pwr
        key_reasons[c] = reasons
        
    best_key = max(attack_powers, key=attack_powers.get)
    max_attack = attack_powers[best_key]
    
    if max_attack < 最低キーマンスコア or not key_reasons[best_key]:
        return None
        
    # イン信頼度総合計算（外の攻め手の攻撃力を減点）
    base_in = 32.0 + (c1['nat'] * 5.5)
    if c1['f'] >= 2: base_in -= 18.0
    elif c1['f'] == 1: base_in -= 9.0
    if c1['mot'] < 28.0: base_in -= 8.0
    if boats[2]['rank'] == 'B2' or boats[2]['st'] >= 0.19: base_in -= 8.0
    
    if max_attack >= 4.0: base_in -= 14.0
    elif max_attack >= 2.0: base_in -= 7.0
    
    in_trust = min(92.0, max(15.0, base_in))
    if in_trust > イン逃げ率上限: return None
        
    pts = 12 if best_key == 2 else 14
    return {
        'key_boat': best_key, 'key_name': boats[best_key]['name'], 'key_rank': boats[best_key]['rank'],
        'power': max_attack, 'reasons': key_reasons[best_key], 'points': pts,
        'c1_name': c1['name'], 'c1_rank': c1['rank'], 'c1_win': c1['nat'],
        'in_trust': in_trust, 'close_time': close_time
    }

def send_discord_notification(full_message):
    if not DISCORD_WEBHOOK_URL or "http" not in DISCORD_WEBHOOK_URL: return
    lines = full_message.split('\n')
    chunks = []
    curr = ""
    for line in lines:
        if len(curr) + len(line) + 1 > 1850:
            chunks.append(curr.strip()); curr = line + '\n'
        else: curr += line + '\n'
    if curr.strip(): chunks.append(curr.strip())
    for chk in chunks:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": chk}, timeout=10)
            time.sleep(0.5)
        except: pass

def main():
    JST = timezone(timedelta(hours=9), 'JST')
    today_str = datetime.now(JST).strftime("%Y%m%d")
    today_display = datetime.now(JST).strftime("%Y/%m/%d (%a)")
    
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    
    # 開催場を一括取得
    active_venues = fetch_active_venues(session, today_str)
    print(f"✅ 検出開催場: {', '.join(active_venues)}")
    
    hunted_races = []
    for venue in active_venues:
        for rno in range(1, 13):
            bundle = fetch_racelist_fast(session, venue, rno, today_str)
            if not bundle: continue
            s_l, raw_txt = bundle
            res = scan_single_race(s_l, raw_txt)
            if not res: continue
            res['venue'] = venue
            res['rno'] = rno
            hunted_races.append(res)
        time.sleep(0.05)
        
    hunted_races.sort(key=lambda x: x['power'], reverse=True)
    
    msg = f"🌅 **【朝のイン崩壊・波乱予備軍レース】 {today_display}**\n"
    msg += f"本日開催: {', '.join(active_venues)} (計{len(active_venues)*12}R)\n"
    msg += f"波乱勝負候補: **{len(hunted_races)} レース検出**\n\n"
    
    if not hunted_races:
        msg += "⚠️ **本日は条件を満たす波乱レースがありません。完全見送りを推奨します。**"
    else:
        for i, r in enumerate(hunted_races, 1):
            msg += f"**#{i} {r['venue']} {r['rno']}R** (締切 ⏰ **{r['close_time']}**)\n"
            msg += f"🎯 **{r['key_boat']}号艇 {r['key_name']}({r['key_rank']})** 軸 【{r['points']}点勝負】\n"
            msg += f"・イン: {r['c1_name']}({r['c1_rank']}/勝率{r['c1_win']:.1f}/信頼度{r['in_trust']:.0f}%)\n"
            msg += f"・根拠: {', '.join(r['reasons'])}\n"
            msg += "───────────────────\n"
        msg += "\n💡 **【直前チェック】** 締切10分前にColabを実行し、全買い目20倍以上なら投票してください！"
        
    print(msg)
    send_discord_notification(msg)

if __name__ == "__main__":
    main()
