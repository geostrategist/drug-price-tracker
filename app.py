"""
Drug Price Tracking System – Streamlit Web App
===============================================
Tabs:
  1. PPP 比較表  – PPP-corrected Taiwan reference price vs NHI price
  2. 藥品搜尋    – search across all sources
  3. 總覽圖表    – charts

Sidebar controls:
  - Data source status + fetch buttons
  - PPP settings (α, exchange rate)
"""
import logging
import io
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import db
import ppp as ppp_module
from scrapers import japan_mhlw, who_ems, oecd_health, worldbank_gdp, taiwan_nhi

# Log to a file so logs persist across Streamlit reruns
LOG_FILE = Path(__file__).parent / "data" / "scraper.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(io.StringIO()),   # suppress stderr
    ],
    force=True,
)

# ── page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="藥價追蹤系統",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="expanded",
)

db.init_db()

SCRAPERS = {
    "worldbank": ("🌐 World Bank GDP PPP",  worldbank_gdp.fetch),
    "nhi":       ("🇹🇼 台灣健保藥價",        taiwan_nhi.fetch),
    "who":       ("🏥 WHO/HAI 藥品調查",      who_ems.fetch),
    "mhlw":      ("🇯🇵 日本厚生勞動省",       japan_mhlw.fetch),
    "oecd":      ("📊 OECD 藥品支出",         oecd_health.fetch),
}

# ── cached queries ─────────────────────────────────────────────────────────────


@st.cache_data(ttl=300, show_spinner=False)
def _source_status() -> list:
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT name, last_fetched,
               (SELECT COUNT(*) FROM prices   WHERE source_id=sources.id) AS n_prices,
               CASE WHEN name LIKE '%World Bank%'
                    THEN (SELECT COUNT(*) FROM gdp_ppp)
                    ELSE (SELECT COUNT(*) FROM prices WHERE source_id=sources.id)
               END AS n
               FROM sources ORDER BY name"""
        ).fetchall()
    return [dict(r) for r in rows]


@st.cache_data(ttl=300, show_spinner=False)
def _ppp_table(drug_name: str, alpha: float, usd_twd: float) -> pd.DataFrame:
    with db.get_conn() as conn:
        return ppp_module.build_comparison_table(
            conn,
            drug_name=drug_name or None,
            alpha=alpha,
            usd_twd=usd_twd,
        )


@st.cache_data(ttl=300, show_spinner=False)
def _search(q: str) -> pd.DataFrame:
    pat = f"%{q}%"
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT d.name_en, d.name_ja AS name_zh, d.generic_name,
                      d.strength, p.country, p.price, p.currency,
                      p.effective_date, s.name AS source
               FROM drugs d
               JOIN prices  p ON p.drug_id = d.id
               JOIN sources s ON s.id      = p.source_id
               WHERE d.name_en      LIKE ? COLLATE NOCASE
                  OR d.name_ja      LIKE ?
                  OR d.generic_name LIKE ? COLLATE NOCASE
               ORDER BY d.name_en, p.country
               LIMIT 300""",
            (pat, pat, pat),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


# ── helpers ────────────────────────────────────────────────────────────────────


def _run_scraper(key: str) -> None:
    label, fn = SCRAPERS[key]
    # Record log file size before running so we can show only new lines
    log_start = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    with st.spinner(f"正在抓取 {label} …"):
        try:
            with db.get_conn() as conn:
                fn(conn)
            st.success(f"✅ {label} 完成")
        except Exception as exc:
            st.error(f"❌ {label} 失敗：{exc}")
    # Show only the log lines produced by this run
    if LOG_FILE.exists():
        with open(LOG_FILE, encoding="utf-8", errors="replace") as _lf:
            _lf.seek(log_start)
            new_logs = _lf.read()
        if new_logs.strip():
            with st.expander("📋 執行記錄", expanded=("error" in new_logs.lower() or "failed" in new_logs.lower())):
                st.code(new_logs, language="")
    st.cache_data.clear()



# ── sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("💊 藥價追蹤系統")
    st.caption("PPP 校正跨國藥價比較平台(成淵高中)")

    st.divider()
    st.subheader("資料狀態")
    statuses = _source_status()
    if statuses:
        for s in statuses:
            icon = "✅" if s["last_fetched"] else "⚪"
            st.caption(f"{icon} **{s['name']}**  \n{s['last_fetched'] or '未下載'} · {s['n']:,} 筆")
        # Show Taiwan GDP PPP status specifically
        with db.get_conn() as _c:
            tw_row = _c.execute(
                "SELECT gdp_ppp_usd, year FROM gdp_ppp WHERE country_code='TWN' ORDER BY year DESC LIMIT 1"
            ).fetchone()
        if tw_row:
            st.caption(f"✅ **台灣 GDP PPP**  \n{tw_row['year']} · ${tw_row['gdp_ppp_usd']:,.0f}")
        else:
            st.caption("⚪ **台灣 GDP PPP** 未取得")
    else:
        st.info("尚無資料，請先點擊「抓取全部」。")

    st.divider()
    st.subheader("更新資料")

    if st.button("⬇️ 抓取全部", use_container_width=True, type="primary"):
        for key in SCRAPERS:
            _run_scraper(key)
        st.rerun()

    with st.expander("個別更新"):
        for key, (label, _) in SCRAPERS.items():
            if st.button(label, key=f"btn_{key}", use_container_width=True):
                _run_scraper(key)
                st.rerun()

    st.divider()
    st.subheader("PPP 設定")
    alpha = st.slider(
        "彈性係數 α",
        min_value=0.3, max_value=1.0,
        value=float(ppp_module.ALPHA), step=0.05,
        help="台灣參考價 = 外國藥價 × (TW_GDP / 來源國_GDP)^α\n"
             "α=0.6 為 Shih et al. 建議值",
    )
    usd_twd = st.number_input(
        "USD / TWD 匯率", min_value=28.0, max_value=40.0,
        value=31.5, step=0.1, format="%.1f",
    )


# ── main ───────────────────────────────────────────────────────────────────────

st.title("💊 跨國藥價 PPP 校正比較")
tab_ppp, tab_search, tab_chart, tab_debug = st.tabs(["📊 PPP 比較表", "🔍 藥品搜尋", "📈 總覽圖表", "🔧 診斷"])


# ═══ Tab 1: PPP 比較表 ═════════════════════════════════════════════════════════

with tab_ppp:
    st.markdown(
        """
        **公式：** `台灣參考價(USD)` = `外國原價(USD)` × (`台灣 GDP_PPP` / `來源國 GDP_PPP`)^α

        | 顏色 | 意義 |
        |------|------|
        | 🟢 綠色 | 健保價 < PPP參考價（偏低 > 15%）|
        | 🟡 黃色 | 健保價 ≈ PPP參考價（±15% 內）|
        | 🔴 紅色 | 健保價 > PPP參考價（偏高 > 15%）|
        """
    )

    drug_filter = st.text_input(
        "篩選藥品名稱", placeholder="e.g. aspirin, amoxicillin …",
        label_visibility="collapsed",
    )

    with st.spinner("計算中 …"):
        df_ppp = _ppp_table(drug_filter, alpha, usd_twd)

    if df_ppp.empty:
        st.warning("沒有可用資料。請先在側邊欄點擊「**⬇️ 抓取全部**」。")
        st.info(
            "**建議抓取順序**\n"
            "1. 🌐 World Bank GDP PPP — PPP 係數來源\n"
            "2. 🇹🇼 台灣健保藥價 — 健保現行支付標準\n"
            "3. 🏥 WHO/HAI 或 🇯🇵 日本厚生勞動省 — 外國參考價"
        )
    else:
        show_cols = ["藥品名", "比對方式", "來源國", "原價_USD", "TW_ref_USD",
                     "TW_ref_TWD", "健保價_TWD", "差異_%"]
        show_cols = [c for c in show_cols if c in df_ppp.columns]
        display_df = df_ppp[show_cols].copy()

        # Format numeric columns as strings for clean display
        for col, fmt in [("原價_USD", "{:.4f}"), ("TW_ref_USD", "{:.4f}"),
                         ("TW_ref_TWD", "{:.2f}"), ("健保價_TWD", "{:.2f}")]:
            if col in display_df.columns:
                display_df[col] = display_df[col].apply(
                    lambda x: fmt.format(x) if pd.notna(x) else "—"
                )
        if "差異_%" in display_df.columns:
            display_df["差異_%"] = display_df["差異_%"].apply(
                lambda x: f"{x:+.1f}%" if pd.notna(x) else "—"
            )

        st.dataframe(display_df, use_container_width=True, height=520)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("比較筆數", f"{len(df_ppp):,}")
        matched = df_ppp["差異_%"].notna().sum()
        c2.metric("有健保對應", f"{matched:,}")
        if matched:
            avg = df_ppp["差異_%"].dropna().mean()
            pos = (df_ppp["差異_%"] > 15).sum()
            neg = (df_ppp["差異_%"] < -15).sum()
            c3.metric("平均差異", f"{avg:+.1f}%")
            c4.metric("偏高 🔴 / 偏低 🟢", f"{pos} / {neg}")

        csv_bytes = df_ppp.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button(
            "⬇️ 下載完整表格 (CSV)", csv_bytes,
            file_name="ppp_comparison.csv", mime="text/csv",
        )


# ═══ Tab 2: 藥品搜尋 ═══════════════════════════════════════════════════════════

with tab_search:
    q = st.text_input(
        "搜尋",
        placeholder="輸入藥品名稱（英文、中文、日文、學名均可）",
        label_visibility="collapsed",
    )
    if q:
        with st.spinner("搜尋中 …"):
            df_s = _search(q)
        if df_s.empty:
            st.info(f"找不到「{q}」的結果。")
        else:
            st.success(f"找到 {len(df_s):,} 筆")
            st.dataframe(df_s, use_container_width=True, height=450)
            csv2 = df_s.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button(
                "⬇️ 下載搜尋結果", csv2,
                file_name="search_result.csv", mime="text/csv",
            )
    else:
        st.caption("請在上方輸入關鍵字開始搜尋。")


# ═══ Tab 3: 總覽圖表 ════════════════════════════════════════════════════════════

with tab_chart:
    with st.spinner("載入圖表 …"):
        df_c = _ppp_table(None, alpha, usd_twd)

    if df_c.empty:
        st.warning("請先抓取資料。")
    else:
        # ── Chart 1: Diff distribution ──────────────────────────────────────
        diff_data = df_c["差異_%"].dropna()
        if not diff_data.empty:
            st.subheader("差異分布（健保價 vs PPP 參考價）")
            fig1 = px.histogram(
                diff_data, nbins=50,
                labels={"value": "差異 %", "count": "筆數"},
                color_discrete_sequence=["#4B9CD3"],
                title="健保價與 PPP 參考價的差異分布",
            )
            fig1.add_vline(
                x=0, line_dash="dash", line_color="red",
                annotation_text="等值線", annotation_position="top right",
            )
            fig1.update_layout(showlegend=False)
            st.plotly_chart(fig1, use_container_width=True)

        # ── Chart 2: Box plot by country ────────────────────────────────────
        if "TW_ref_TWD" in df_c.columns:
            df_box = df_c[df_c["TW_ref_TWD"].notna()]
            if not df_box.empty:
                st.subheader("各來源國 PPP 校正後台灣參考價分布")
                fig2 = px.box(
                    df_box, x="來源國", y="TW_ref_TWD",
                    color="來源國",
                    title="各來源國 PPP 校正後參考價（TWD）",
                    labels={"TW_ref_TWD": "PPP 參考價 (TWD)", "來源國": "來源國"},
                )
                fig2.update_layout(showlegend=False)
                st.plotly_chart(fig2, use_container_width=True)

        # ── Chart 3: Scatter GDP vs price ───────────────────────────────────
        if "國家GDP_PPP" in df_c.columns:
            df_sc = df_c[df_c["國家GDP_PPP"].notna() & df_c["原價_USD"].notna()]
            if not df_sc.empty:
                st.subheader("國家 GDP PPP vs 藥品原始售價")
                fig3 = px.scatter(
                    df_sc, x="國家GDP_PPP", y="原價_USD",
                    color="來源國", hover_data=["藥品名"],
                    title="國家 GDP per capita PPP vs 藥品原價（USD）",
                    labels={
                        "國家GDP_PPP": "GDP per capita PPP (USD)",
                        "原價_USD": "藥品原價 (USD)",
                    },
                    log_y=True, opacity=0.7,
                )
                st.plotly_chart(fig3, use_container_width=True)


# ═══ Tab 4: 診斷 ════════════════════════════════════════════════════════════════

with tab_debug:
    st.subheader("資料庫狀態")
    with db.get_conn() as _conn:
        # Table row counts
        tables = ["sources", "drugs", "prices", "gdp_ppp"]
        cols = st.columns(len(tables))
        for i, t in enumerate(tables):
            n = _conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            cols[i].metric(t, f"{n:,}")

        # Taiwan GDP PPP
        st.markdown("**台灣 GDP PPP (gdp_ppp 表)**")
        tw_rows = _conn.execute(
            "SELECT country_code, country_name, year, gdp_ppp_usd FROM gdp_ppp "
            "WHERE country_code='TWN' ORDER BY year DESC LIMIT 5"
        ).fetchall()
        if tw_rows:
            st.dataframe(pd.DataFrame([dict(r) for r in tw_rows]), use_container_width=True)
        else:
            st.error("❌ gdp_ppp 表中沒有台灣資料 (TWN)。請重新抓取 World Bank 資料。")

        # Japan sample
        st.markdown("**日本藥價樣本（前 10 筆有 price_usd 的）**")
        jp_rows = _conn.execute(
            "SELECT d.name_ja, p.price, p.currency, p.price_usd, p.country "
            "FROM prices p JOIN drugs d ON d.id=p.drug_id "
            "WHERE p.country='JPN' AND p.price_usd IS NOT NULL LIMIT 10"
        ).fetchall()
        if jp_rows:
            st.dataframe(pd.DataFrame([dict(r) for r in jp_rows]), use_container_width=True)
        else:
            st.warning("找不到日本藥價的 price_usd 欄位資料。")

        # Taiwan NHI sample
        st.markdown("**台灣健保藥價樣本（前 10 筆）**")
        nhi_rows = _conn.execute(
            "SELECT d.name_ja, d.name_en, p.price, p.currency "
            "FROM prices p JOIN drugs d ON d.id=p.drug_id "
            "WHERE p.country='TWN' LIMIT 10"
        ).fetchall()
        if nhi_rows:
            st.dataframe(pd.DataFrame([dict(r) for r in nhi_rows]), use_container_width=True)
        else:
            st.warning("找不到台灣健保藥價資料（country='TWN'）。")

    st.markdown("**執行記錄**")
    if LOG_FILE.exists():
        with open(LOG_FILE, encoding="utf-8", errors="replace") as _lf:
            all_logs = _lf.read()
        if all_logs.strip():
            st.code(all_logs[-4000:], language="")
        else:
            st.caption("記錄檔為空，請先執行抓取。")
    else:
        st.caption("尚無記錄，請先執行抓取。")
