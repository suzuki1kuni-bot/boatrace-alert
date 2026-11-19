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

# 場ごとの地元支部マッピング（B1地元イン+6.26pt判定用）
LOCAL_BRANCH = {
    1: "群馬", 2: "埼玉", 3: "東京", 4: "東京", 5: "東京", 6: "静岡",
    7: "愛知", 8: "愛知", 9: "三重", 10: "福井", 11: "滋賀", 12: "大阪",
    13: "兵庫", 14: "徳島", 15: "香川", 16: "岡山", 17: "広島", 18: "山口",
    19: "山口", 20: "福岡", 21: "福岡", 22: "福岡", 23: "佐賀", 24: "長崎"
}

# 場特性分類（モジュール5）
NARROW_VENUES = {2, 3, 4}       # 戸田・江戸川・平和島（狭い水面・イン不利）
WIDE_VENUES = {18, 21, 24}      # 徳山・芦屋・大村（広い水面・イン60%超）
FRESH_VENUES = {1, 2, 5, 11}    # 主な淡水（桐生・戸田・多摩川・びわこ ※住之江/尼崎等も含むが代表例）

BRANCH_REGEX = re.compile(
    r'(群馬|埼玉|東京|静岡|愛知|三重|福井|滋賀|大阪|兵庫|徳島|香川|岡山|広島|山口|福岡|佐賀|長崎)\s*/'
)

MAX_TARGET_RACES = 15  # 1日の厳選出力上限

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
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd:02d}&hd={date_str}"
    try:
        res = SESSION.get(url, timeout=15)
        res.raise_for_status()
        res.encoding = res.apparent_encoding
        if "データがありません" in res.text:
            return None, "NO_DATA"
        soup = BeautifulSoup(res.text, 'html.parser')
        if not soup.find('title'):
            return None, f"ERROR: Empty HTML (len={len(res.text)})"
        return soup, "OK"
    except Exception as e:
        msg = f"ERROR: {e}"
        print(f"⚠️ 取得エラー (場:{jcd:02d} {rno}R): {msg}")
        return None, msg

def parse_deadlines(soup):
    deadlines = {}
    for tr in soup.find_all('tr'):
        if '締切予定時刻' in tr.get_text():
            tds = [td for td in tr.find_all('td') if '締切' not in td.get_text()]
            for idx, td in enumerate(tds):
                if idx + 1 <= 12:
                    deadlines[idx + 1] = td.get_text(strip=True)
            break
    return deadlines

def parse_boat_data(tbody):
    tds = tbody.find_all('td')
    if len(tds) < 7:
        return None

    cell2 = tds[2].get_text(separator=' ').strip()
    rank_match = re.search(r'(A1|A2|B1|B2)', cell2)
    rank = rank_match.group(1) if rank_match else "B1"

    branch_match = BRANCH_REGEX.search(cell2)
    branch = branch_match.group(1) if branch_match else ""

    cell3 = tds[3].get_text(separator=' ').strip()
    f_match = re.search(r'F(\d)', cell3)
    f_count = int(f_match.group(1)) if f_match else 0

    st_match = re.search(r'(0\.\d{2})', cell3)
    avg_st = float(st_match.group(1)) if st_match else 0.20

    nat_text = tds[4].get_text(separator=' ').strip().split()
    nat_win = safe_float(nat_text[0]) if len(nat_text) > 0 else 0.0

    loc_text = tds[5].get_text(separator=' ').strip().split()
    loc_win = safe_float(loc_text[0]) if len(loc_text) > 0 else 0.0

    mot_text = tds[6].get_text(separator=' ').strip().split()
    mot_2q = safe_float(mot_text[1]) if len(mot_text) > 1 else 0.0

    return {
        "rank": rank,
        "branch": branch,
        "f_count": f_count,
        "avg_st": avg_st,
        "nat_win": nat_win,
        "loc_win": loc_win,
        "mot_2q": mot_2q
    }

def evaluate_quant_race(jcd, rno, deadline, boats):
    """
    クオンツ事前精査ロジック
    戻り値: None（足切り） または スコアと要約辞書
    """
    if len(boats) != 6:
        return None
    if not deadline or "中止" in deadline:
        return None

    b1 = boats[0]
    venue_name = VENUE_DICT[jcd]
    local_branch = LOCAL_BRANCH.get(jcd, "")
    is_b1_local = (b1["branch"] == local_branch and local_branch != "")

    score = 0
    tags = []
    details = []

    # 環境タグ
    env_tag = "狭" if jcd in NARROW_VENUES else ("広" if jcd in WIDE_VENUES else ("淡" if jcd in FRESH_VENUES else "並"))

    # 外枠の脅威確認（ST差・強破壊者）
    outer_min_st = min(b["avg_st"] for b in boats[1:4])  # 2〜4コース最速ST
    st_gap = round(b1["avg_st"] - outer_min_st, 2)       # プラスなら1号艇がST劣勢

    has_4_destroyer = (boats[3]["rank"] == "A1" or (boats[3]["nat_win"] >= 6.3 and boats[3]["mot_2q"] >= 38.0))
    has_outer_destroyer = any(
        b["nat_win"] >= 6.3 and b["mot_2q"] >= 38.0 for b in boats[4:6]
    )

    # 1. 【逆張りイン高EV期待】チェック
    if b1["rank"] == "B1" and is_b1_local and b1["f_count"] == 0 and b1["avg_st"] <= 0.16:
        if jcd in WIDE_VENUES or b1["nat_win"] >= 4.5:
            score += 4
            tags.append("逆張ｲﾝ")
            details.append("B1地元(+6.26pt)")

    if b1["rank"] in ["A1", "A2"] and b1["f_count"] == 0:
        if (0 < b1["mot_2q"] < 30.0) or (b1["nat_win"] >= 5.5 and b1["loc_win"] < b1["nat_win"] - 1.5):
            score += 3
            if "逆張ｲﾝ" not in tags:
                tags.append("逆張ｲﾝ")
            if 0 < b1["mot_2q"] < 30.0:
                details.append(f"M{b1['mot_2q']}%")
            if b1["loc_win"] < b1["nat_win"] - 1.5:
                details.append(f"全{b1['nat_win']}/当{b1['loc_win']}")

    # 2. 【イン崩壊・荒れ歪み】チェック
    if jcd in NARROW_VENUES and b1["rank"] in ["B1", "B2"]:
        if b1["nat_win"] < 5.2 or b1["avg_st"] >= 0.17 or b1["f_count"] >= 1:
            score += 4
            tags.append("ｲﾝ崩壊")
            details.append(f"{b1['rank']}(全{b1['nat_win']})")

    if b1["f_count"] >= 1:
        score += 3
        if "ｲﾝ崩壊" not in tags:
            tags.append("ｲﾝ崩壊")
        details.append(f"F{b1['f_count']}")

    if st_gap >= 0.03:
        score += 3
        if "ｲﾝ崩壊" not in tags:
            tags.append("ｲﾝ崩壊")
        details.append(f"ST差+{st_gap:.2f}")

    # 3. 【大衆罠・決まり手ロック】チェック
    if b1["rank"] in ["B1", "B2"] and has_4_destroyer:
        score += 2
        tags.append("4ｶﾄﾞ罠")
        details.append(f"4:{boats[3]['rank']}({boats[3]['nat_win']})")

    if jcd in WIDE_VENUES and has_outer_destroyer:
        score += 3
        tags.append("5・6ﾛｯｸ")

    # 基本情報の補足（1号艇スペック重複整理）
    base_b1 = f"1:{b1['rank']}/ST{b1['avg_st']:.2f}/M{b1['mot_2q']}%"

    # 最低ライン（スコア3未満は凡戦ノイズとして足切り）
    if score < 3:
        return None

    # ランク判定
    rank_label = "S" if score >= 6 else "A"
    tag_str = "".join(f"[{t}]" for t in tags[:2])
    detail_str = " ".join(details[:2])

    # ソート用の時刻キー（HH:MM形式でない場合は99:99）
    sort_time = deadline if re.match(r'^\d{1,2}:\d{2}$', deadline) else "99:99"
    if len(sort_time) == 4:
        sort_time = "0" + sort_time

    line = f"[{deadline}][{rank_label}/{env_tag}]{venue_name}{rno}R {base_b1} {tag_str} {detail_str}".strip()

    return {
        "score": score,
        "sort_time": sort_time,
        "line": line
    }

def send_to_discord(race_lines, is_error=False):
    if not DISCORD_WEBHOOK_URL or "http" not in DISCORD_WEBHOOK_URL:
        print("⚠️ Webhook URLが正しく設定されていません。")
        return

    if is_error:
        header = "🚨 **Morning Scanner: システム取得エラー検知** 🚨\n"
    else:
        header = "📊 **Quant Pre-Screener: 本日の特選ターゲット（締切順）**\n"

    max_len = 900
    chunks = []
    current_chunk = header

    for line in race_lines:
        row = line + "\n"
        if len(current_chunk) + len(row) > max_len:
            chunks.append(current_chunk.strip())
            current_chunk = row
        else:
            current_chunk += row

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    total = len(chunks)
    for i, chunk in enumerate(chunks):
        part_prefix = f"**[{i+1}/{total}]**\n" if total > 1 and not is_error else ""
        payload = part_prefix + chunk
        if len(payload) > 1950:
            payload = payload[:1950]
        try:
            res = requests.post(DISCORD_WEBHOOK_URL, json={"content": payload}, timeout=10)
            if res.status_code not in [200, 204]:
                print(f"⚠️ Discord送信エラー (Status: {res.status_code}): {res.text}")
            else:
                print(f"✅ Discord送信成功 (チャンク {i+1}/{total})")
            time.sleep(1.5)
        except Exception as e:
            print(f"⚠️ Discord通信例外: {e}")

# ---------------------------------------------------------
# 🚀 メイン処理
# ---------------------------------------------------------
def main():
    JST = timezone(timedelta(hours=9), 'JST')
    today_str = datetime.now(JST).strftime("%Y%m%d")

    print(f"🔍 クオンツ事前スキャン開始: {today_str}")
    candidates = []
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

        deadlines = parse_deadlines(soup_1r)
        # 全レース中止判定（1R〜12Rの締切欄がすべて中止の場合等）
        if deadlines and all("中止" in v for v in deadlines.values()):
            print(f"全レース中止: {venue_name}")
            time.sleep(0.3)
            continue

        active_venues += 1
        print(f"✅ 開催確認: {venue_name}")

        for rno in range(1, 13):
            deadline = deadlines.get(rno, "--:--")
            if "中止" in deadline:
                continue

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
                continue

            boats = []
            for w in range(6):
                b_data = parse_boat_data(tbodys[w])
                if b_data:
                    boats.append(b_data)

            res = evaluate_quant_race(jcd, rno, deadline, boats)
            if res:
                candidates.append(res)
                print(f"  🎯 特選候補 (Score {res['score']}): {res['line']}")

            time.sleep(0.4)

    # スコア上位 MAX_TARGET_RACES 件を抽出し、締切時刻順へソート
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = candidates[:MAX_TARGET_RACES]
    selected.sort(key=lambda x: x["sort_time"])

    print(f"\nスキャン完了 | 開催場数: {active_venues}場 | 候補総数: {len(candidates)} | 厳選出力: {len(selected)}件 | エラー: {len(error_logs)}件")

    if active_venues == 0:
        err_summary = error_logs[:5] if error_logs else ["全24場でデータ未取得（日付またはHTML変更の可能性）"]
        send_to_discord(err_summary, is_error=True)
    elif selected:
        send_to_discord([item["line"] for item in selected])
    else:
        msg = [f"✅ 本日はクオンツ事前基準（Score>=3）を満たす特選ターゲットはありません。（確認済み開催場: {active_venues}場）"]
        if error_logs:
            msg.append(f"⚠️ 一部取得エラーあり ({len(error_logs)}件): {error_logs[0]}")
        send_to_discord(msg)

if __name__ == "__main__":
    main()
