import os
import json
import time
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

CFN_ID = "Ccookie"
DATA_FILE = "data.json"

LOGIN_URL = "https://cid.capcom.com/ja/login/?guidedBy=web"
AUTH_URL = "https://www.streetfighter.com/6/buckler/auth/loginep?redirect_url=/"
BATTLE_LOG_URL = f"https://www.streetfighter.com/6/buckler/profile/{CFN_ID}/battlelog/rank"


def get_credentials():
    email = os.environ.get("CAPCOM_EMAIL")
    password = os.environ.get("CAPCOM_PASSWORD")
    if not email or not password:
        raise ValueError("CAPCOM_EMAIL and CAPCOM_PASSWORD environment variables must be set")
    return email, password


def authenticate(page, email, password):
    page.goto(LOGIN_URL)
    page.wait_for_load_state("networkidle")

    # Handle age verification if present
    if page.locator("input[name='birth_year']").count() > 0:
        page.fill("input[name='birth_year']", "1990")
        page.fill("input[name='birth_month']", "01")
        page.fill("input[name='birth_day']", "01")
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")

    page.fill("input[type='email'], input[name='email'], input[id='email']", email)
    page.fill("input[type='password'], input[name='password'], input[id='password']", password)
    page.click("button[type='submit']")

    # Wait for redirect to buckler
    for _ in range(30):
        if "buckler" in page.url:
            return True
        time.sleep(1)

    return False


def extract_next_data(page):
    content = page.content()
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', content, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group(1))


def character_id_to_name(char_id):
    characters = {
        1: "Ryu", 2: "Luke", 3: "Kimberly", 4: "Chun-Li", 5: "Manon",
        6: "Zangief", 7: "JP", 8: "Dhalsim", 9: "Cammy", 10: "Ken",
        11: "Dee Jay", 12: "Lily", 13: "A.K.I.", 14: "Rashid",
        15: "Blanka", 16: "Juri", 17: "Marisa", 18: "Guile",
        19: "Ed", 20: "Akuma", 21: "M. Bison", 22: "Terry",
        23: "Mai", 24: "Elena",
    }
    return characters.get(char_id, f"Unknown({char_id})")


def league_from_lp(lp):
    if lp >= 25000: return "Master"
    if lp >= 20000: return "Diamond"
    if lp >= 14000: return "Platinum"
    if lp >= 9000: return "Gold"
    if lp >= 5000: return "Silver"
    if lp >= 1000: return "Bronze"
    return "Rookie"


def is_victory(round_results):
    losses = sum(1 for r in round_results if r == 0)
    return losses <= 1


def fetch_battle_log(page):
    page.goto(BATTLE_LOG_URL)
    page.wait_for_load_state("networkidle")
    data = extract_next_data(page)
    if not data:
        raise RuntimeError("Could not extract __NEXT_DATA__ from battle log page")
    return data["props"]["pageProps"]["battle_log"]


def parse_matches(battle_log, existing_replay_ids):
    replay_list = battle_log.get("replay_list", [])
    fighter_banner = battle_log.get("fighter_banner_info", {})
    my_fighter_id = fighter_banner.get("personal_info", {}).get("fighter_id", "")

    new_matches = []
    for replay in replay_list:
        replay_id = replay.get("replay_id", "")
        if replay_id in existing_replay_ids:
            continue

        p1 = replay.get("player1_info", {})
        p2 = replay.get("player2_info", {})

        # Determine which side is the tracked player
        if p1.get("fighter_id") == my_fighter_id or p1.get("player", {}).get("fighter_id") == my_fighter_id:
            me = p1
            opponent = p2
        else:
            me = p2
            opponent = p1

        my_char_id = me.get("character_id")
        opp_char_id = opponent.get("character_id")
        my_lp = me.get("league_point", 0)
        opp_lp = opponent.get("league_point", 0)
        my_mr = me.get("master_rating", 0)
        opp_mr = opponent.get("master_rating", 0)
        round_results = me.get("round_results", [])
        victory = is_victory(round_results)
        opp_name = opponent.get("player", {}).get("fighter_id", "Unknown")

        uploaded_at = replay.get("uploaded_at", "")
        try:
            dt = datetime.fromtimestamp(uploaded_at)
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M")
        except Exception:
            date_str = ""
            time_str = ""

        new_matches.append({
            "replay_id": replay_id,
            "date": date_str,
            "time": time_str,
            "character": character_id_to_name(my_char_id),
            "opponent": opp_name,
            "opponent_character": character_id_to_name(opp_char_id),
            "victory": victory,
            "lp": my_lp,
            "lp_gain": None,
            "mr": my_mr,
            "mr_gain": None,
            "opponent_lp": opp_lp,
            "opponent_mr": opp_mr,
            "opponent_league": league_from_lp(opp_lp),
        })

    return new_matches


def compute_lp_gains(matches):
    # Sort oldest first to compute gains in order
    sorted_matches = sorted(matches, key=lambda m: (m["date"], m["time"]))
    for i in range(1, len(sorted_matches)):
        prev_lp = sorted_matches[i - 1]["lp"]
        curr_lp = sorted_matches[i]["lp"]
        sorted_matches[i]["lp_gain"] = curr_lp - prev_lp
        if sorted_matches[i - 1]["mr"] and sorted_matches[i]["mr"]:
            sorted_matches[i]["mr_gain"] = sorted_matches[i]["mr"] - sorted_matches[i - 1]["mr"]
    return sorted_matches


def load_existing_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"players": {}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def main():
    email, password = get_credentials()
    existing_data = load_existing_data()

    player_data = existing_data["players"].get(CFN_ID, {"matches": []})
    existing_replay_ids = {m["replay_id"] for m in player_data["matches"]}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print(f"Authenticating as {email}...")
        ok = authenticate(page, email, password)
        if not ok:
            print("Authentication failed")
            browser.close()
            return

        print(f"Fetching battle log for {CFN_ID}...")
        battle_log = fetch_battle_log(page)
        browser.close()

    new_matches = parse_matches(battle_log, existing_replay_ids)
    print(f"Found {len(new_matches)} new matches")

    all_matches = player_data["matches"] + new_matches
    all_matches = compute_lp_gains(all_matches)

    existing_data["players"][CFN_ID] = {
        "cfn_id": CFN_ID,
        "last_updated": datetime.utcnow().isoformat(),
        "matches": all_matches,
    }

    save_data(existing_data)
    print(f"Saved {len(all_matches)} total matches for {CFN_ID}")


if __name__ == "__main__":
    main()
