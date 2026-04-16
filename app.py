import os
import threading
import streamlit as st
import requests
import pandas as pd
import io
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="FB Campaign Inspector", page_icon="📊", layout="wide")

# ── Token ──────────────────────────────────────────────────────────────────────
default_token = ""
token_path = os.path.join(os.path.dirname(__file__), "token.txt")
if os.path.exists(token_path):
    try:
        with open(token_path, "r") as f:
            default_token = f.read().strip()
    except:
        pass

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap');

html, body, [class*="css"] {
    font-family: 'Syne', sans-serif;
}
.stApp {
    background: #0a0a0f;
    color: #e2e8f0;
}
.info-note {
    font-size: 12px;
    color: #94a3b8;
    background: #111827;
    border: 1px dashed #334155;
    border-radius: 6px;
    padding: 8px 14px;
    margin-top: 6px;
    font-family: 'JetBrains Mono', monospace;
}
.metric-card {
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 16px;
    text-align: center;
}
.campaign-status {
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    color: #38bdf8;
    background: #0c1628;
    border-left: 3px solid #0ea5e9;
    padding: 8px 12px;
    border-radius: 0 6px 6px 0;
    margin: 4px 0;
}
.error-item {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
}
</style>
""", unsafe_allow_html=True)


# ── Thread-safe session state lock ────────────────────────────────────────────
_lock = threading.Lock()


# ── API ────────────────────────────────────────────────────────────────────────

def api_get(url, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                err = data["error"]
                code = err.get("code", "")
                msg = err.get("message", "Unknown error")
                # Rate limit: back off and retry
                if code in (17, 32, 4, 613) or "rate limit" in msg.lower():
                    import time
                    time.sleep(2 ** attempt)
                    continue
                raise Exception(f"FB API {code}: {msg}")
            return data
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                continue
            raise Exception("Request timed out after multiple attempts.")
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                continue
            raise Exception("Connection error. Please check your internet.")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network error: {e}")
    return None


def get_campaign_info(campaign_id, token):
    return api_get(
        f"https://graph.facebook.com/v19.0/{campaign_id}",
        {"fields": "id,name,status,objective,daily_budget,lifetime_budget,budget_remaining,start_time,updated_time",
         "access_token": token}
    )


def fmt_fb_time(val):
    """Convert FB ISO timestamp to readable local string, or return as-is."""
    if not val:
        return "—"
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return val


def get_adsets(campaign_id, token):
    url = f"https://graph.facebook.com/v19.0/{campaign_id}/adsets"
    params = {
        "fields": (
            "id,name,status,targeting,promoted_object,daily_budget,lifetime_budget,budget_remaining,"
            "start_time,updated_time,"
            "creative{object_story_spec,link_url},"
            "ads{creative{object_story_spec,link_url,asset_feed_spec}}"
        ),
        "access_token": token,
        "limit": 100,
    }
    results = []
    while url:
        data = api_get(url, params)
        if data is None:
            break
        results.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = {}
    return results


# ── ROAS ───────────────────────────────────────────────────────────────────────

def get_campaign_roas(campaign_id, token, date_start, date_end):
    since = date_start if isinstance(date_start, str) else date_start.strftime("%Y-%m-%d")
    until = date_end if isinstance(date_end, str) else date_end.strftime("%Y-%m-%d")
    try:
        data = api_get(
            f"https://graph.facebook.com/v19.0/{campaign_id}/insights",
            {
                "fields": "spend,purchase_roas",
                "time_range": f'{{"since":"{since}","until":"{until}"}}',
                "access_token": token,
                "level": "campaign",
            }
        )
        rows = (data or {}).get("data", [])
        if not rows:
            return None
        roas_list = rows[0].get("purchase_roas", [])
        if roas_list:
            total = sum(float(r.get("value", 0)) for r in roas_list if r.get("value"))
            return round(total, 4) if total else None
        return None
    except Exception:
        return None


def fetch_roas_windows(campaign_id, token, d1):
    d_yesterday = d1 - timedelta(days=1)
    d_3day = d1 - timedelta(days=2)
    d_7day = d1 - timedelta(days=6)

    # Fetch all 4 windows in parallel using a mini thread pool
    results = {}
    windows = {
        "roas_today": (d1, d1),
        "roas_yesterday": (d_yesterday, d_yesterday),
        "roas_3day": (d_3day, d1),
        "roas_7day": (d_7day, d1),
    }
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(get_campaign_roas, campaign_id, token, s, e): k
            for k, (s, e) in windows.items()
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception:
                results[key] = None
    return results


def fmt_roas(val):
    if val is None:
        return "N/A"
    return f"{val:.2f}x"


# ── Parsers ────────────────────────────────────────────────────────────────────

def parse_audiences(targeting):
    inc = [a.get("name", a.get("id", "")) for a in targeting.get("custom_audiences", [])]
    exc = [a.get("name", a.get("id", "")) for a in targeting.get("excluded_custom_audiences", [])]
    return " | ".join(filter(None, inc)), " | ".join(filter(None, exc))


def extract_urls_from_story_spec(spec):
    urls = set()
    if not isinstance(spec, dict):
        return urls
    for key in ["link_data", "video_data", "photo_data"]:
        block = spec.get(key, {})
        if isinstance(block, dict):
            for field in ["link", "url", "call_to_action"]:
                val = block.get(field)
                if isinstance(val, str) and val.startswith("http"):
                    urls.add(val)
                elif isinstance(val, dict):
                    inner = val.get("value", {})
                    if isinstance(inner, dict):
                        link = inner.get("link") or inner.get("url")
                        if link and link.startswith("http"):
                            urls.add(link)
    return urls


def parse_urls(adset):
    urls = set()
    po = adset.get("promoted_object") or {}
    if po.get("url"):
        urls.add(po["url"])
    rule = po.get("pixel_rule") or {}
    if isinstance(rule, dict):
        for v in rule.get("url", {}).values():
            if isinstance(v, list):
                urls.update(v)
            elif isinstance(v, str) and v.startswith("http"):
                urls.add(v)
    creative = adset.get("creative") or {}
    if creative.get("link_url"):
        urls.add(creative["link_url"])
    urls |= extract_urls_from_story_spec(creative.get("object_story_spec") or {})
    ads_data = (adset.get("ads") or {}).get("data") or []
    for ad in ads_data:
        ad_creative = ad.get("creative") or {}
        if ad_creative.get("link_url"):
            urls.add(ad_creative["link_url"])
        urls |= extract_urls_from_story_spec(ad_creative.get("object_story_spec") or {})
        afs = ad_creative.get("asset_feed_spec") or {}
        for link_obj in afs.get("link_urls", []):
            if isinstance(link_obj, dict):
                for field in ["website_url", "display_url"]:
                    v = link_obj.get(field)
                    if v and v.startswith("http"):
                        urls.add(v)
    return " | ".join(sorted(urls)) if urls else ""


# ── Excel Reader ───────────────────────────────────────────────────────────────

def read_excel(file):
    try:
        df = pd.read_excel(file, dtype=str, header=0)
    except Exception as e:
        raise Exception(f"Could not read file: {e}")

    df.columns = [str(c).strip() for c in df.columns]
    if df.shape[1] < 2:
        raise Exception("Need at least 2 columns: Account Name, Campaign ID.")

    cols = list(df.columns)
    rename_map = {cols[0]: "Account Name", cols[1]: "Campaign ID"}
    has_date_col = df.shape[1] >= 3
    if has_date_col:
        rename_map[cols[2]] = "Date (D1)"

    df = df.rename(columns=rename_map)
    keep = ["Account Name", "Campaign ID"] + (["Date (D1)"] if has_date_col else [])
    df = df[keep].dropna(subset=["Campaign ID"]).copy()
    df["Campaign ID"] = df["Campaign ID"].str.strip()
    df["Account Name"] = df["Account Name"].fillna("Unknown").str.strip()
    df = df[df["Campaign ID"] != ""].reset_index(drop=True)

    if has_date_col:
        df["Date (D1)"] = pd.to_datetime(df["Date (D1)"], errors="coerce").dt.date
        df["Date (D1)"] = df["Date (D1)"].fillna(datetime.today().date())
    else:
        df["Date (D1)"] = datetime.today().date()

    if df.empty:
        raise Exception("No valid rows found.")
    return df


# ── Excel Export — Campaign ID in EVERY sheet ─────────────────────────────────

def get_excel_io(data_df, opts, include_roas):
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        # All Data sheet
        data_df.to_excel(writer, sheet_name="All Data", index=False)

        if "Audiences" in opts:
            cols = ["Account Name", "Campaign ID", "Campaign Name", "Ad Set ID", "Ad Set Name",
                    "Inclusions (C1)", "Exclusions (C2)"]
            existing = [c for c in cols if c in data_df.columns]
            data_df[existing].to_excel(writer, sheet_name="Audiences", index=False)

        if "Website URLs" in opts:
            cols = ["Account Name", "Campaign ID", "Campaign Name", "Ad Set ID", "Ad Set Name",
                    "Website URL(s)"]
            existing = [c for c in cols if c in data_df.columns]
            data_df[existing].to_excel(writer, sheet_name="Website URLs", index=False)

        if "Budgets" in opts:
            cols = ["Account Name", "Campaign ID", "Campaign Name",
                    "Campaign Start Time", "Campaign Updated Time",
                    "Campaign Budget",
                    "Ad Set ID", "Ad Set Name", "Ad Set Budget", "Ad Set Status",
                    "Ad Set Start Time", "Ad Set Updated Time"]
            existing = [c for c in cols if c in data_df.columns]
            data_df[existing].to_excel(writer, sheet_name="Budgets", index=False)

        if include_roas:
            roas_cols = [
                "Account Name", "Campaign ID", "Campaign Name", "Date (D1)",
                "ROAS Today (D1)", "ROAS Yesterday (D1-1)",
                "ROAS 3-Day (D1-2→D1)", "ROAS 7-Day (D1-6→D1)"
            ]
            existing = [c for c in roas_cols if c in data_df.columns]
            roas_df = (
                data_df[existing].drop_duplicates(subset=["Campaign ID"])
                if "Campaign ID" in existing else data_df[existing]
            )
            roas_df.to_excel(writer, sheet_name="ROAS", index=False)

        # Timestamps sheet — always included
        ts_cols = [
            "Account Name", "Campaign ID", "Campaign Name",
            "Campaign Start Time", "Campaign Updated Time",
            "Ad Set ID", "Ad Set Name",
            "Ad Set Start Time", "Ad Set Updated Time",
        ]
        existing_ts = [c for c in ts_cols if c in data_df.columns]
        data_df[existing_ts].to_excel(writer, sheet_name="Timestamps", index=False)

    out.seek(0)
    return out


# ── Core per-campaign fetch (runs in thread) ───────────────────────────────────

def fetch_one_campaign(account, cid, d1, token, col_options, fetch_roas, roas_cache):
    """
    Returns (list_of_rows, error_str_or_None).
    roas_cache is a dict shared across threads; writes are protected inside.
    """
    rows = []
    errors = []

    # ── ROAS (4 parallel sub-fetches) ─────────────────────────────────────────
    roas_data = {"roas_today": None, "roas_yesterday": None,
                 "roas_3day": None, "roas_7day": None}
    if fetch_roas:
        cache_key = f"{cid}__{d1}"
        with _lock:
            cached = roas_cache.get(cache_key)
        if cached:
            roas_data = cached
        else:
            try:
                roas_data = fetch_roas_windows(cid, token, d1)
                with _lock:
                    roas_cache[cache_key] = roas_data
            except Exception as e:
                errors.append(f"ROAS fetch for Campaign `{cid}` ({account}): {e}")

    # ── Campaign + Ad Sets ────────────────────────────────────────────────────
    try:
        camp = get_campaign_info(cid, token)
        if not camp:
            raise Exception("Campaign data not found or API error.")

        adsets = get_adsets(cid, token)

        # Budget string
        camp_budget_str = "Not Fetched"
        if "Budgets" in col_options:
            if camp.get("daily_budget"):
                camp_budget_str = f"Daily: {int(camp['daily_budget'])/100:.2f}"
            elif camp.get("lifetime_budget"):
                camp_budget_str = f"Lifetime: {int(camp['lifetime_budget'])/100:.2f}"
            else:
                camp_budget_str = "Not Set"

        def build_row(adset=None):
            r = {
                "Account Name": account,
                "Campaign ID": cid,                          # Always included
                "Campaign Name": camp.get("name", ""),
                "Campaign Status": camp.get("status", ""),
                "Campaign Start Time": fmt_fb_time(camp.get("start_time")),
                "Campaign Updated Time": fmt_fb_time(camp.get("updated_time")),
                "Date (D1)": str(d1),
                "Ad Set ID": adset.get("id", "") if adset else "—",
                "Ad Set Name": adset.get("name", "") if adset else "—",
                "Ad Set Status": adset.get("status", "") if adset else "—",
                "Ad Set Start Time": fmt_fb_time(adset.get("start_time")) if adset else "—",
                "Ad Set Updated Time": fmt_fb_time(adset.get("updated_time")) if adset else "—",
            }
            if "Budgets" in col_options:
                r["Campaign Budget"] = camp_budget_str
                if adset:
                    if adset.get("daily_budget"):
                        r["Ad Set Budget"] = f"Daily: {int(adset['daily_budget'])/100:.2f}"
                    elif adset.get("lifetime_budget"):
                        r["Ad Set Budget"] = f"Lifetime: {int(adset['lifetime_budget'])/100:.2f}"
                    else:
                        r["Ad Set Budget"] = "Inherited/Not Set"
                else:
                    r["Ad Set Budget"] = "—"

            if "Audiences" in col_options:
                if adset:
                    targeting = adset.get("targeting") or {}
                    inc, exc = parse_audiences(targeting)
                    r["Inclusions (C1)"] = inc if inc else "None"
                    r["Exclusions (C2)"] = exc if exc else "None"
                else:
                    r["Inclusions (C1)"] = "None"
                    r["Exclusions (C2)"] = "None"

            if "Website URLs" in col_options:
                if adset:
                    url_str = parse_urls(adset)
                    r["Website URL(s)"] = url_str if url_str else "N/A"
                else:
                    r["Website URL(s)"] = "N/A"

            if fetch_roas:
                r["ROAS Today (D1)"] = fmt_roas(roas_data.get("roas_today"))
                r["ROAS Yesterday (D1-1)"] = fmt_roas(roas_data.get("roas_yesterday"))
                r["ROAS 3-Day (D1-2→D1)"] = fmt_roas(roas_data.get("roas_3day"))
                r["ROAS 7-Day (D1-6→D1)"] = fmt_roas(roas_data.get("roas_7day"))

            return r

        if not adsets:
            rows.append(build_row(adset=None))
        else:
            for adset in adsets:
                rows.append(build_row(adset=adset))

    except Exception as e:
        errors.append(f"Campaign `{cid}` ({account}): {e}")

    return rows, errors


# ── UI ─────────────────────────────────────────────────────────────────────────

st.title("📊 FB Campaign Inspector")
st.caption("Parallel fetching — audiences, URLs, budgets & ROAS — Campaign ID in every export sheet.")
st.divider()

col_a, col_b = st.columns(2, gap="large")

with col_a:
    st.subheader("🔐 Access Token")
    access_token = st.text_input(
        "Facebook Ads Access Token",
        type="password",
        value=default_token,
        placeholder="EAAxxxxxxxxxxxxxxx...",
        help="Requires ads_read + ads_management permission.",
    )

with col_b:
    st.subheader("📂 Campaign List (Excel)")
    st.markdown(
        '<div class="info-note">Columns: <b>Account Name</b> | <b>Campaign ID</b> | '
        '<b>Date (D1)</b> (optional — defaults to today)</div>',
        unsafe_allow_html=True
    )
    uploaded_file = st.file_uploader(
        "Upload Excel", type=["xlsx", "xls"], label_visibility="collapsed"
    )

st.divider()

col_options = st.multiselect(
    "🎯 Data Fields to Fetch",
    options=["Audiences", "Website URLs", "Budgets", "ROAS"],
    default=["Audiences", "Website URLs", "Budgets", "ROAS"],
)

fetch_roas = "ROAS" in col_options

# Thread concurrency slider
max_workers = st.slider(
    "⚡ Parallel Campaign Workers",
    min_value=1, max_value=10, value=5,
    help="Higher = faster but more likely to hit FB rate limits. 3–5 is the sweet spot."
)

if fetch_roas:
    st.info(
        "📈 **ROAS Windows** — Today (D1), Yesterday (D1−1), 3-Day (D1−2→D1), 7-Day (D1−6→D1). "
        "All 4 windows are fetched in parallel per campaign.",
        icon="ℹ️"
    )

st.divider()

# ── Session State ──────────────────────────────────────────────────────────────
if "all_rows" not in st.session_state:
    st.session_state.all_rows = []
if "roas_cache" not in st.session_state:
    st.session_state.roas_cache = {}


def get_excel_download(rows, opts, include_roas):
    df = pd.DataFrame(rows)
    return get_excel_io(df, opts, include_roas)


if st.session_state.all_rows:
    c1, c2 = st.columns([3, 1])
    with c1:
        st.download_button(
            "📥 Download Current Progress (Excel)",
            data=get_excel_download(st.session_state.all_rows, col_options, fetch_roas),
            file_name="fb_campaign_progress.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    with c2:
        if st.button("🗑️ Reset", use_container_width=True):
            st.session_state.all_rows = []
            st.session_state.roas_cache = {}
            st.rerun()

st.divider()

campaigns_df = None
if uploaded_file:
    try:
        campaigns_df = read_excel(uploaded_file)
        st.success(f"✅ {len(campaigns_df)} campaign(s) loaded")
        st.dataframe(campaigns_df, use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"File error: {e}")

fetch_btn = st.button("🚀 Fetch All Campaigns", type="primary", use_container_width=True)

# ── Parallel Fetch ─────────────────────────────────────────────────────────────

if fetch_btn:
    if not (access_token and access_token.strip()):
        st.error("Access token is required.")
        st.stop()
    if campaigns_df is None:
        st.error("Upload a campaign Excel file first.")
        st.stop()

    token = access_token.strip()
    total = len(campaigns_df)
    all_errors = []

    st.divider()
    progress_bar = st.progress(0)
    status_text = st.empty()
    live_log = st.empty()
    completed_count = [0]   # mutable for thread-safe increment
    log_lines = []

    # Build task list
    tasks = [
        (row["Account Name"], row["Campaign ID"], row["Date (D1)"])
        for _, row in campaigns_df.iterrows()
    ]

    # Submit all to thread pool
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(
                fetch_one_campaign,
                account, cid, d1,
                token, col_options, fetch_roas,
                st.session_state.roas_cache
            ): (account, cid)
            for account, cid, d1 in tasks
        }

        for future in as_completed(future_to_task):
            account, cid = future_to_task[future]
            try:
                rows, errs = future.result()
                with _lock:
                    st.session_state.all_rows.extend(rows)
                    all_errors.extend(errs)
                    completed_count[0] += 1
                    cnt = completed_count[0]

                # Update UI (Streamlit main thread)
                pct = cnt / total
                progress_bar.progress(min(pct, 1.0))
                status_text.markdown(
                    f"⏳ **{cnt} / {total} completed** — "
                    f"Last: `{account}` · `{cid}` · "
                    f"{len(rows)} ad set(s) fetched"
                )
                log_lines.insert(0, f"✅ `{account}` | `{cid}` — {len(rows)} ad set(s)")
                # Show last 8 lines
                live_log.markdown("\n\n".join(log_lines[:8]))

            except Exception as e:
                with _lock:
                    all_errors.append(f"Thread error for Campaign `{cid}` ({account}): {e}")
                    completed_count[0] += 1

            # Auto-save every 20 completions
            cnt = completed_count[0]
            if cnt % 20 == 0 or cnt == total:
                if st.session_state.all_rows:
                    try:
                        temp_df = pd.DataFrame(st.session_state.all_rows)
                        temp_df.to_csv("fb_fetch_autosave.csv", index=False)
                        with pd.ExcelWriter("fb_fetch_autosave.xlsx", engine="openpyxl") as writer:
                            temp_df.to_excel(writer, index=False)
                    except:
                        pass

    progress_bar.progress(1.0)
    status_text.markdown(f"✅ **Done! {total} campaign(s) processed using {max_workers} parallel workers.**")
    st.info("💡 Auto-saved to `fb_fetch_autosave.xlsx` in the application folder.")

    if all_errors:
        with st.expander(f"⚠️ {len(all_errors)} error(s) — click to expand"):
            for err in all_errors:
                st.error(err)

    if st.session_state.all_rows:
        result_df = pd.DataFrame(st.session_state.all_rows)

        st.subheader(f"📋 {len(result_df)} Ad Set(s) across {total} Campaign(s)")

        # ── Metrics row ────────────────────────────────────────────────────────
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Campaigns", total)
        with m2:
            st.metric("Ad Sets", len(result_df))
        with m3:
            ok = total - len(all_errors)
            st.metric("Successful", ok)
        with m4:
            st.metric("Errors", len(all_errors))

        # ── Tabs ───────────────────────────────────────────────────────────────
        tab_list = []
        if "Audiences" in col_options:
            tab_list.append("🎯 Audiences")
        if "Website URLs" in col_options:
            tab_list.append("🌐 Website URLs")
        if "Budgets" in col_options:
            tab_list.append("💰 Budgets")
        if fetch_roas:
            tab_list.append("📈 ROAS")
        tab_list.append("🕐 Timestamps")
        tab_list.append("📄 All Columns")

        tabs = st.tabs(tab_list)
        tab_idx = 0

        if "Audiences" in col_options:
            with tabs[tab_idx]:
                cols = ["Account Name", "Campaign ID", "Campaign Name",
                        "Ad Set ID", "Ad Set Name", "Inclusions (C1)", "Exclusions (C2)"]
                existing = [c for c in cols if c in result_df.columns]
                st.dataframe(result_df[existing], use_container_width=True, hide_index=True)
            tab_idx += 1

        if "Website URLs" in col_options:
            with tabs[tab_idx]:
                cols = ["Account Name", "Campaign ID", "Campaign Name",
                        "Ad Set ID", "Ad Set Name", "Website URL(s)"]
                existing = [c for c in cols if c in result_df.columns]
                st.dataframe(result_df[existing], use_container_width=True, hide_index=True)
            tab_idx += 1

        if "Budgets" in col_options:
            with tabs[tab_idx]:
                cols = ["Account Name", "Campaign ID", "Campaign Name",
                        "Campaign Start Time", "Campaign Updated Time",
                        "Campaign Budget",
                        "Ad Set ID", "Ad Set Name", "Ad Set Budget", "Ad Set Status",
                        "Ad Set Start Time", "Ad Set Updated Time"]
                existing = [c for c in cols if c in result_df.columns]
                st.dataframe(result_df[existing], use_container_width=True, hide_index=True)
            tab_idx += 1

        if fetch_roas:
            with tabs[tab_idx]:
                roas_cols = [
                    "Account Name", "Campaign ID", "Campaign Name", "Date (D1)",
                    "ROAS Today (D1)", "ROAS Yesterday (D1-1)",
                    "ROAS 3-Day (D1-2→D1)", "ROAS 7-Day (D1-6→D1)"
                ]
                existing = [c for c in roas_cols if c in result_df.columns]
                roas_view = result_df[existing].drop_duplicates(subset=["Campaign ID"])
                st.dataframe(roas_view, use_container_width=True, hide_index=True)
            tab_idx += 1

        # Timestamps tab — always shown
        with tabs[tab_idx]:
            ts_cols = [
                "Account Name", "Campaign ID", "Campaign Name",
                "Campaign Start Time", "Campaign Updated Time",
                "Ad Set ID", "Ad Set Name",
                "Ad Set Start Time", "Ad Set Updated Time",
            ]
            existing_ts = [c for c in ts_cols if c in result_df.columns]
            st.dataframe(result_df[existing_ts], use_container_width=True, hide_index=True)
        tab_idx += 1

        with tabs[tab_idx]:
            st.dataframe(result_df, use_container_width=True, hide_index=True)

        st.divider()
        st.download_button(
            label="⬇️ Final Download — Full Results (Excel, Campaign ID in every sheet)",
            data=get_excel_io(result_df, col_options, fetch_roas),
            file_name="fb_campaign_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True
        )
    else:
        st.warning("No data retrieved. Verify your access token and campaign IDs.")

st.divider()
st.caption("Facebook Ads API v19.0 · ads_read + ads_management · Parallel workers via ThreadPoolExecutor")
