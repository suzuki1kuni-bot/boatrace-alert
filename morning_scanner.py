import requests
from bs4 import BeautifulSoup
import re
import os
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------
# ⚙️ 設定・定数
# ---------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

VENUES = [
    "桐生", "戸田", "江戸川", "平和島", "多摩川", "浜名湖", "蒲郡", "常滑", "津", "三国", "びわこ", "住之江",
    "尼崎", "鳴門", "丸亀", "児島", "宮島", "徳山", "下関", "若松", "芦屋", "福岡", "唐津", "大村"
]
VENUE_DICT = {i + 1: name for i, name in enumerate(VENUES)}

# HTTPリクエスト用のセッションと標準ブラウザヘッダー
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache'
})

# ---------------------------------------------------------
# 🛠️ ユーティリティ関数
# ---------------------------------------------------------
def safe_float(text, default=0.0):
    try:
        clean = re.sub(r'[^\d.]', '', str(text))
        return float(clean) if clean else default
    except Exception:
        return default

def fetch_racelist(jcd, rno, date_str):
    """
    戻り値: (BeautifulSoupオブジェクト or None, ステータス文字列)
    ステータス: "OK", "NO_DATA", "ERROR: <詳細>"
    """
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd:02d}&hd={date_str}"
    try:
        res = SESSION.get(url, timeout=15)
        res.raise_for_status()
        res.encoding = res.apparent_encoding
        if "データがありません" in res.text:
            return None, "NO_DATA"
        soup = BeautifulSoup(res.text, 'html.parser')
        # 正常な出走表ページかどうかの最低限の検証
        if not soup.find('title'):
            return None, f"ERROR: Empty HTML (len={len(res.text)})"
        return soup, "OK"
    except Exception as e:
        msg = f"ERROR: {e}"
        print(f"⚠️ 取得エラー (場:{jcd:02d} {rno}R): {msg}")
        return None, msg

def parse_boat_data(tbody):
    tds = tbody.find_all('td')
    if len(tds) < 7:
        return None

    rank_match = re.search(r'(A1|A2|B1|B2)', tds[2].get_text())
    rank = rank_match.group(1) if rank_match else "B1"

    f_match = re.search(r'F(\d)', tds[3].get_text())
    f_count = int(f_match.group(1)) if f_match else 0

    nat_text = tds[4].get_text(separator=' ').strip().split()
    nat_win = safe_float(nat_text[0]) if len(nat_text) > 0 else 0.0

    loc_text = tds[5].get_text(separator=' ').strip().split()
    loc_win = safe_float(loc_text[0]) if len(loc_text) > 0 else 0.0

    mot_text = tds[6].get_text(separator=' ').strip().split()
    mot_2q = safe_float(mot_text[1]) if len(mot_text) > 1 else 0.0

    return {
        "rank": rank,
        "f_count": f_count,
        "nat_win": nat_win,
        "loc_win": loc_win,
        "mot_2q": mot_2q
    }

def analyze_race(boats):
    if len(boats) != 6:
        return []
    b1 = boats[0]
    reasons = []

    if b1["rank"] in ["B1", "B2"]:
        reasons.append("🎯 1号艇がB級")
    if b1["f_count"] >= 1:
        reasons.append(f"⚠️ 1号艇が今節F{b1['f_count']}持ち")
    if b1["mot_2q"] > 0 and b1["mot_2q"] < 30.0:
        reasons.append(f"🔧 1号艇のモーター低調 (2連対率 {b1['mot_2q']}%)")
    if b1["nat_win"] > 0 and b1["loc_win"] < (b1["nat_win"] - 1.0):
        reasons.append(f"🌊 1号艇の当地勝率極低 (全国 {b1['nat_win']} / 当地 {b1['loc_win']})")

    if b1["nat_win"] < 5.0:
        for i in range(1, 5):
            b = boats[i]
            if b["nat_win"] >= 6.5 and b["mot_2q"] >= 40.0:
                reasons.append(f"⚔️ {i+1}号艇に強破壊者 (勝率 {b['nat_win']} / モーター {b['mot_2q']}%)")
                break

    return reasons

def send_to_discord(race_list, is_error=False):
    if not DISCORD_WEBHOOK_URL or "http" not in DISCORD_WEBHOOK_URL:
        print("⚠️ Webhook URLが正しく設定されていません。")
        return

    if is_error:
        header = "🚨 **Morning Scanner: システム取得エラー検知** 🚨\n\n"
    else:
        header = "🚤 **Morning Scanner: 本日のイン崩壊予兆レース** 🚨\n*(※直前クオンツ分析のターゲット候補)*\n\n"

    body = "\n".join(race_list)
    content = header + body

    chunks = []
    if len(content) > 1900:
        current_chunk = header
        for race in race_list:
            if len(current_chunk) + len(race) + 1 > 1900:
                chunks.append(current_chunk)
                current_chunk = race + "\n"
            else:
                current_chunk += race + "\n"
        if current_chunk:
            chunks.append(current_chunk)
    else:
        chunks.append(content)

    for i, chunk in enumerate(chunks):
        try:
            res = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk.strip()}, timeout=10)
            if res.status_code not in [200, 204]:
                print(f"⚠️ Discord送信エラー (Status: {res.status_code}): {res.text}")
            else:
                print(f"✅ Discord送信成功 (チャンク {i+1}/{len(chunks)})")
            time.sleep(1)
        except Exception as e:
            print(f"⚠️ Discord通信例外: {e}")

# ---------------------------------------------------------
# 🚀 メイン処理
# ---------------------------------------------------------
def main():
    JST = timezone(timedelta(hours=9), 'JST')
    today_str = datetime.now(JST).strftime("%Y%m%d")

    print(f"🔍 スキャン開始: {today_str}")
    target_races = []
    active_venues = 0
    error_logs = []

    for jcd in range(1, 25):
        venue_name = VENUE_DICT[jcd]

        soup_1r, status_1r = fetch_racelist(jcd, 1, today_str)
        if status_1r != "OK":
            if status_1r.startswith("ERROR"):
                error_logs.append(f"{venue_name}: {status_1r}")
            else:
                print(f"休催 or データ未公開: {venue_name}")
            time.sleep(0.5)
            continue

        active_venues += 1
        print(f"✅ 開催確認: {venue_name}")

        for rno in range(1, 13):
            if rno == 1:
                soup, status = soup_1r, status_1r
            else:
                soup, status = fetch_racelist(jcd, rno, today_str)

            if status != "OK" or not soup:
                if status.startswith("ERROR"):
                    error_logs.append(f"{venue_name} {rno}R: {status}")
                time.sleep(0.3)
                continue

            tbodys = soup.find_all('tbody', class_='is-fs12')
            if len(tbodys) < 6:
                error_logs.append(f"{venue_name} {rno}R: HTML構造不一致 (tbodys={len(tbodys)})")
                continue

            boats = []
            for w in range(6):
                b_data = parse_boat_data(tbodys[w])
                if b_data:
                    boats.append(b_data)

            reasons = analyze_race(boats)
            if reasons:
                race_info = f"**【{venue_name} {rno}R】**\n" + "\n".join(f"  └ {r}" for r in reasons)
                target_races.append(race_info)
                print(f"  🎯 抽出: {venue_name} {rno}R")

            time.sleep(0.4)

    print(f"\nスキャン完了 | 開催場数: {active_venues}場 | 抽出: {len(target_races)}レース | エラー: {len(error_logs)}件")

    if active_venues == 0:
        err_summary = error_logs[:5] if error_logs else ["全24場でデータ未取得（日付またはHTML変更の可能性）"]
        send_to_discord(err_summary, is_error=True)
    elif target_races:
        send_to_discord(target_races)
    else:
        msg = [f"✅ 本日はイン崩壊の予兆（監視対象）レースはありません。（確認済み開催場: {active_venues}場）"]
        if error_logs:
            msg.append(f"⚠️ 一部取得エラーあり ({len(error_logs)}件): {error_logs[0]}")
        send_to_discord(msg)

if __name__ == "__main__":
    main()
