import requests, re, sys, time, itertools, math, os
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

# =========================================================
# ⚙️ 設定エリア
# =========================================================
# 先ほど取得したDiscordのWebhook URLを貼り付けます（環境変数からも取得可能）
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "ここにDiscordのWebhook_URLを貼り付け")

最低キーマンスコア = 2.5
イン逃げ率上限 = 65.0

VENUES = "桐生 戸田 江戸川 平和島 多摩川 浜名湖 蒲郡 常滑 津 三国 びわこ 住之江 尼崎 鳴門 丸亀 児島 宮島 徳山 下関 若松 芦屋 福岡 唐津 大村".split()
VENUE_DICT = {v: f"{i+1:02d}" for i, v in enumerate(VENUES)}

def fetch_racelist_fast(session, venue, rno, date_str):
    jcd = VENUE_DICT.get(venue)
    u = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={date_str}"
    try:
        r = session.get(u, timeout=8)
        if "データがありません" in r.text: return None
        return BeautifulSoup(r.text, 'html.parser'), r.text
    except: return None

def scan_single_race(s_l, raw_txt):
    tb_l = s_l.find_all('tbody', class_='is-fs12')[:6]
    if len(tb_l) < 6: return None
    
    # 締切予定時刻の取得
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
        
    c1, c2 = boats[1], boats[2]
    
    # 1. イン崩壊リスク判定 (イン逃げ率 <= 65%)
    in_trust = min(92.0, max(25.0, 32.0 + (c1['nat'] * 7.0)))
    if in_trust > イン逃げ率上限: return None
    
    # 2. キーマン特定判定 (2〜4号艇に限定)
    key_candidates = {}
    key_reasons = {}
    for c_idx in [2, 3, 4]:
        c_info = boats[c_idx]
        power = 0.0
        reasons = []
        if c_info['rank'] in ['A1', 'A2'] and c1['rank'] in ['B1', 'B2']:
            power += 2.5; reasons.append(f"格上{c_info['rank']}")
        elif c_info['rank'] == 'A1':
            power += 1.5; reasons.append("実力A1")
        if c_info['mot'] >= 40.0:
            power += 1.5; reasons.append(f"好機{c_info['mot']:.1f}%")
        if c_info['st'] <= c1['st'] - 0.02:
            power += 1.2; reasons.append(f"ST優勢({c_info['st']:.2f})")
            
        pos_w = 1.2 if c_idx in [3, 4] else 1.1
        power *= pos_w
        key_candidates[c_idx] = power
        key_reasons[c_idx] = reasons
        
    best_key = max(key_candidates, key=key_candidates.get)
    best_power = key_candidates[best_key]
    
    if best_power < 最低キーマンスコア or not key_reasons[best_key]:
        return None
        
    pts = 12 if best_key == 2 else 14
    return {
        'key_boat': best_key, 'key_name': boats[best_key]['name'], 'key_rank': boats[best_key]['rank'],
        'power': best_power, 'reasons': key_reasons[best_key], 'points': pts,
        'c1_name': c1['name'], 'c1_rank': c1['rank'], 'c1_win': c1['nat'],
        'close_time': close_time
    }

def send_discord_notification(message):
    if "http" not in DISCORD_WEBHOOK_URL:
        print("⚠️ Discord Webhook URLが設定されていません。コンソール出力のみ行います。")
        return
    payload = {"content": message}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Discord通知エラー: {e}")

def main():
    # 日本時間（JST）の当日日付を取得
    JST = timezone(timedelta(hours=9), 'JST')
    today_str = datetime.now(JST).strftime("%Y%m%d")
    today_display = datetime.now(JST).strftime("%Y/%m/%d (%a)")
    
    print(f"🌅 【朝の全国一括スキャン開始】: {today_display}")
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    
    # 本日開催場を高速検出
    active_venues = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(session.get, f"https://www.boatrace.jp/owpc/pc/race/racelist?rno=1&jcd={VENUE_DICT[v]}&hd={today_str}", timeout=8): v for v in VENUES}
        for f in futures:
            v = futures[f]
            try:
                if "データがありません" not in f.result().text: active_venues.append(v)
            except: pass
                
    active_venues.sort(key=lambda x: VENUES.index(x))
    print(f"✅ 本日の開催場（{len(active_venues)}場）: {', '.join(active_venues)}")
    
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
        time.sleep(0.1)
        
    hunted_races.sort(key=lambda x: x['power'], reverse=True)
    
    # メッセージの組み立て
    msg = f"🌅 **【朝のイン崩壊・波乱予備軍レース】 {today_display}**\n"
    msg += f"全国 {len(active_venues)*12}R 中、イン崩壊シグナル＆キーマン特定: **{len(hunted_races)} レース**\n\n"
    
    if not hunted_races:
        msg += "⚠️ **本日は条件を満たすレースがありません。完全見送りを推奨します。**"
    else:
        for i, r in enumerate(hunted_races, 1):
            msg += f"**#{i} {r['venue']} {r['rno']}R** (締切 ⏰ **{r['close_time']}**)\n"
            msg += f"🎯 **{r['key_boat']}号艇 {r['key_name']}({r['key_rank']})** 軸 【{r['points']}点勝負】\n"
            msg += f"・イン: {r['c1_name']}({r['c1_rank']}/勝率{r['c1_win']:.1f})\n"
            msg += f"・根拠: {', '.join(r['reasons'])}\n"
            msg += "───────────────────\n"
        msg += "\n💡 **【直前チェック】** 締切10分前にColabを実行し、全買い目20倍以上なら投票してください！"
        
    print(msg)
    send_discord_notification(msg)
    print("\n✅ スマホへの通知送信が完了しました！")

if __name__ == "__main__":
    main()
