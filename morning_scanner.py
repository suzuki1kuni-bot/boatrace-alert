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

NARROW_VENUES = {2, 3, 4}       # 戸田・江戸川・平和島（狭い水面）
WIDE_VENUES = {18, 21, 24}      # 徳山・芦屋・大村（広い水面・イン60%超）
FRESH_VENUES = {1, 2, 5, 11}    # 主な淡水

BRANCH_REGEX = re.compile(
    r'(群馬|埼玉|東京|静岡|愛知|三重|福井|滋賀|大阪|兵庫|徳島|香川|岡山|広島|山口|福岡|佐賀|長崎)\s*/'
)

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

def normalize_time(deadline):
    if re.match(r'^\d{1,2}:\d{2}$', str(deadline)):
        return deadline if len(deadline) == 5 else "0" + deadline
    return "99:99"

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

    # 今節成績のSTと着順を取得（trs[2]: ST行, trs[3]: 着順行）
    recent_sts = []
    recent_ranks = []
    trs = tbody.find_all('tr')
    if len(trs) >= 4:
        for td in trs[2].find_all('td'):
            t = td.get_text(strip=True)
            m = re.search(r'\.(\d{2})', t)
            if m:
                recent_sts.append(float("0." + m.group(1)))
        for td in trs[3].find_all('td'):
            t = td.get_text(strip=True)
            if re.match(r'^[1-6]$', t):
                recent_ranks.append(int(t))

    return {
        "rank": rank,
        "branch": branch,
        "f_count": f_count,
        "avg_st": avg_st,
        "nat_win": nat_win,
        "loc_win": loc_win,
        "mot_2q": mot_2q,
        "recent_sts": recent_sts,
        "recent_ranks": recent_ranks
    }

def evaluate_quant_v3(jcd, rno, deadline, boats):
    """
    Quant Scanner v3: 大衆の錯覚・非対称エッジ特化スクリーナー
    """
    if len(boats) != 6:
        return None
    if not deadline or "中止" in deadline:
        return None

    b1 = boats[0]
    venue_name = VENUE_DICT[jcd]
    local_branch = LOCAL_BRANCH.get(jcd, "")
    is_b1_local = (b1["branch"] == local_branch and local_branch != "")
    is_all_a1 = all(b["rank"] == "A1" for b in boats)

    score = 0
    tags = []
    notes = []

    env_tag = "狭" if jcd in NARROW_VENUES else ("広" if jcd in WIDE_VENUES else ("淡" if jcd in FRESH_VENUES else "並"))
    if is_all_a1:
        env_tag += "/G1"

    # -----------------------------------------------------
    # 1. 【A級イン死角】大衆のA1/A2過信の裏を撃つ
    # -----------------------------------------------------
    if b1["rank"] in ["A1", "A2"] and not is_all_a1:
        sub_score = 0
        sub_notes = []
        if b1["f_count"] >= 1:
            sub_score += 4
            sub_notes.append(f"F{b1['f_count']}")
        if b1["avg_st"] >= 0.17 and any(b["avg_st"] <= 0.14 for b in boats[1:4]):
            sub_score += 3
            sub_notes.append(f"ST劣({b1['avg_st']:.2f})")
        if 0 < b1["mot_2q"] < 28.0:
            sub_score += 2
            sub_notes.append(f"M{b1['mot_2q']}%")
        if b1["loc_win"] > 0 and b1["loc_win"] < (b1["nat_win"] - 1.5):
            sub_score += 2
            sub_notes.append(f"当{b1['loc_win']}")

        # 外枠に攻め手（高モーター or 高ST）がいれば死角完成
        if sub_score >= 2 and any(b["mot_2q"] >= 38.0 or b["avg_st"] <= 0.14 for b in boats[1:4]):
            score += sub_score + 1
            tags.append("A級ｲﾝ死角")
            notes.extend(sub_notes[:2])

    # -----------------------------------------------------
    # 2. 【広水面B1地元先制】大衆のA級信仰の裏（逆張りイン）
    # -----------------------------------------------------
    if jcd in WIDE_VENUES and b1["rank"] == "B1" and is_b1_local:
        has_outer_a = any(b["rank"] in ["A1", "A2"] for b in boats[1:6])
        if has_outer_a and b1["f_count"] == 0 and b1["avg_st"] <= 0.16 and (b1["mot_2q"] >= 30.0 or b1["mot_2q"] == 0):
            score += 5
            tags.append("広B1地元")
            notes.append("外A吸/地元+6.26")

    # -----------------------------------------------------
    # 3. 【壁崩壊→隣先制】相対STギャップ（0.17以上 vs 0.14以下）
    # -----------------------------------------------------
    for wall_idx in [1, 2]:  # 2コース(idx1), 3コース(idx2)
        wall = boats[wall_idx]
        attacker = boats[wall_idx + 1]  # 3コース(idx2) or 4コース(idx3)

        wall_recent_slow = (len(wall["recent_sts"]) >= 2 and (sum(wall["recent_sts"][-2:]) / 2) >= 0.19)
        wall_weak = (wall["avg_st"] >= 0.17 or wall_recent_slow or wall["f_count"] >= 1)

        att_recent_fast = (len(attacker["recent_sts"]) >= 2 and (sum(attacker["recent_sts"][-2:]) / 2) <= 0.13)
        att_strong = (attacker["avg_st"] <= 0.14 or att_recent_fast)
        att_capable = (attacker["nat_win"] >= 5.0 or attacker["mot_2q"] >= 35.0)

        if wall_weak and att_strong and att_capable and (wall["avg_st"] - attacker["avg_st"] >= 0.03 or att_recent_fast):
            add = 4 if attacker["rank"] in ["B1", "A2"] else 3  # 目立ちにくいB1/A2の隣先制をより高く評価
            score += add
            tags.append("壁崩壊")
            notes.append(f"{wall_idx+1}凹→{wall_idx+2}({attacker['rank']})")
            break

    # -----------------------------------------------------
    # 4. 【外枠A級の周辺認知差し】1M渋滞時の真空突入
    # -----------------------------------------------------
    inner_slow_count = sum(1 for b in boats[:3] if b["avg_st"] >= 0.16)
    if inner_slow_count >= 2 or (b1["rank"] in ["B1", "A2"] and boats[1]["rank"] in ["B1", "A2"]):
        for out_idx in [4, 5]:  # 5コース(idx4), 6コース(idx5)
            out_b = boats[out_idx]
            if out_b["rank"] == "A1" or (out_b["rank"] == "A2" and out_b["nat_win"] >= 6.2):
                # 全員A1戦でない場合ほど大衆が5・6を軽視するため加点
                add = 3 if is_all_a1 else 4
                score += add
                tags.append("外A級差")
                notes.append(f"{out_idx+1}:{out_b['rank']}({out_b['nat_win']})")
                break

    # -----------------------------------------------------
    # 5. 【今節ST急上昇の伏兵】古い数字と実戦気配の乖離
    # -----------------------------------------------------
    for idx in range(1, 5):  # 2〜5コース
        b = boats[idx]
        if b["nat_win"] < 5.5 and b["mot_2q"] < 34.0 and len(b["recent_sts"]) >= 2 and len(b["recent_ranks"]) >= 2:
            last2_st = sum(b["recent_sts"][-2:]) / 2
            has_ren = any(r <= 2 for r in b["recent_ranks"][-2:])
            if last2_st <= 0.13 and has_ren:
                score += 3
                tags.append("今節気配")
                notes.append(f"{idx+1}ST{last2_st:.2f}")
                break

    # 閾値判定（ノイズ排除のためスコア4以上を抽出、全員A1戦は5以上）
    min_score = 5 if is_all_a1 else 4
    if score < min_score:
        return None

    rank_label = "S" if score >= 7 else "A"
    base_b1 = f"1:{b1['rank']}(ST{b1['avg_st']:.2f}/M{b1['mot_2q']}%)"
    tag_str = "".join(f"[{t}]" for t in tags[:2])
    note_str = " ".join(notes[:2])
    sort_time = normalize_time(deadline)

    line = f"[{deadline}][{rank_label}/{env_tag}]{venue_name}{rno}R {base_b1} {tag_str} {note_str}".strip()

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
        header = "🚨 **Quant Scanner v3: システム取得エラー検知** 🚨\n"
    else:
        header = "🎯 **Quant Scanner v3: 大衆錯覚ターゲット（締切順）**\n"

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
    now_jst = datetime.now(JST)
    today_str = now_jst.strftime("%Y%m%d")
    now_hm = now_jst.strftime("%H:%M")

    print(f"🔍 Quant Scanner v3 開始: {today_str} (現在時刻 {now_hm} JST)")
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

            sort_time = normalize_time(deadline)
            if sort_time != "99:99" and sort_time < now_hm:
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

            res = evaluate_quant_v3(jcd, rno, deadline, boats)
            if res:
                candidates.append(res)
                print(f"  🎯 抽出 (Score {res['score']}): {res['line']}")

            time.sleep(0.4)

    candidates.sort(key=lambda x: (x["sort_time"], -x["score"]))

    print(f"\nスキャン完了 | 開催場数: {active_venues}場 | 抽出: {len(candidates)}件 | エラー: {len(error_logs)}件")

    if active_venues == 0:
        err_summary = error_logs[:5] if error_logs else ["全24場でデータ未取得（日付またはHTML変更の可能性）"]
        send_to_discord(err_summary, is_error=True)
    elif candidates:
        send_to_discord([item["line"] for item in candidates])
    else:
        msg = [f"✅ 残存レースにQuant Scanner v3基準（Score>=4）を満たす歪み候補はありません。（確認済み開催場: {active_venues}場）"]
        if error_logs:
            msg.append(f"⚠️ 一部取得エラーあり ({len(error_logs)}件): {error_logs[0]}")
        send_to_discord(msg)

if __name__ == "__main__":
    main()
