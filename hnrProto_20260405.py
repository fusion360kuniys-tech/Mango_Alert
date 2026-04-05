import streamlit as st
import pandas as pd
import plotly.express as px
import sqlite3
import serial
import random
import time
import math
from datetime import datetime, timedelta

# ==========================================
# 1. 基本設定
# ==========================================
DEBUG_MODE = True  # 実機接続時は False に変更
SERIAL_PORT = 'COM3' 
BAUD_RATE = 9600
DB_NAME = "greenhouse_data_v3.db"

# 農薬ごとのアラート停止期間（日）の設定
PESTICIDE_SETTINGS = {
    "フジドーL_28日間": 28,
    "ジマンダイゼン水和物_21日間": 21,
    "オーソサイド水和物_21日間": 21,
    "スミレックス水和剤_28日間": 28,
    "アミスター10フロアブル_7日間": 7
}

# セッション状態の初期化
if 'lw_total_seconds' not in st.session_state:
    st.session_state.lw_total_seconds = 0.0
    st.session_state.dry_start_time = None
if 'pesticide_expiry' not in st.session_state:
    st.session_state.pesticide_expiry = None

# ==========================================
# 2. ロジック関数
# ==========================================

def init_db():
    """データベースとテーブルの初期化（threshold_setカラムを追加）"""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, 
                temperature REAL,
                humidity REAL,
                vpd REAL,
                leaf_wetness_min REAL,
                risk_p REAL,
                threshold_set INTEGER
            )
        """)
        # 既存DBへのカラム追加対策（risk_p と threshold_set）
        columns = [("risk_p", "REAL"), ("threshold_set", "INTEGER")]
        for col_name, col_type in columns:
            try:
                conn.execute(f"ALTER TABLE sensor_logs ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

def calculate_p(t_hours, T_temp):
    """病害リスクPの計算"""
    if t_hours <= 0: return 0.0
    try:
        y = -16.114 + (1.12 * T_temp) - (0.0225 * (T_temp**2)) + (1.0862 * math.log(t_hours))
        exp_y = math.exp(y)
        p = (exp_y / (1 + exp_y)) * 100
        return round(p, 2)
    except (OverflowError, ValueError):
        return 100.0 if 'y' in locals() and y > 0 else 0.0

def calculate_vpd(temp, humid):
    """VPD（飽和水蒸気圧差）の計算"""
    svp = 0.61078 * math.exp((17.27 * temp) / (temp + 237.3))
    avp = svp * (humid / 100)
    return round(svp - avp, 2)

def save_to_db(t, h, vpd, lw_min, risk_p, threshold):
    """データの保存（閾値設定も含める）"""
    now_jst = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "INSERT INTO sensor_logs (timestamp, temperature, humidity, vpd, leaf_wetness_min, risk_p, threshold_set) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_jst, t, h, vpd, lw_min, risk_p, threshold)
        )

def load_data_range(start_date, end_date):
    """指定期間のデータを読み込み"""
    with sqlite3.connect(DB_NAME) as conn:
        query = "SELECT * FROM sensor_logs WHERE timestamp BETWEEN ? AND ?"
        df = pd.read_sql_query(
            query, conn, 
            params=(start_date.strftime('%Y-%m-%d 00:00:00'), 
                    end_date.strftime('%Y-%m-%d 23:59:59'))
        )
        if not df.empty:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        return df

# ==========================================
# 3. Streamlit UI 構築
# ==========================================
st.set_page_config(page_title="Greenhouse Risk Monitor", layout="wide")

init_db()

st.title("🌿 マンゴー炭疽病感染リスクアラートシステム")

# --- サイドバー：アラート・計測設定 ---
st.sidebar.header("⚠️ アラート・計測設定")
alert_threshold = st.sidebar.radio("アラート閾値 (P値 %)", [20, 50], index=1)
reset_threshold_min = st.sidebar.number_input("乾燥リセット判定時間 (分)", min_value=1, value=30)

if st.sidebar.button("累積時間を手動リセット"):
    st.session_state.lw_total_seconds = 0.0
    st.session_state.dry_start_time = None
    st.sidebar.success("累積データをクリアしました")

# --- サイドバー：農薬散布管理 ---
st.sidebar.markdown("---")
st.sidebar.header("💊 農薬散布管理")
selected_pesticide = st.sidebar.selectbox("散布した農薬を選択", list(PESTICIDE_SETTINGS.keys()))
if st.sidebar.button(f"{selected_pesticide} の散布を記録"):
    days = PESTICIDE_SETTINGS[selected_pesticide]
    st.session_state.pesticide_expiry = datetime.now() + timedelta(days=days)
    st.sidebar.success(f"{selected_pesticide}（効果:{days}日）を記録しました。")

if st.session_state.pesticide_expiry:
    remaining = st.session_state.pesticide_expiry - datetime.now()
    if remaining.total_seconds() > 0:
        st.sidebar.info(f"🛡 散布効果中: あと {remaining.days}日 {remaining.seconds//3600}時間")
    else:
        st.session_state.pesticide_expiry = None

# --- サイドバー：データエクスポート ---
st.sidebar.markdown("---")
st.sidebar.header("📁 データエクスポート")
today_dt = datetime.now()
start_date = st.sidebar.date_input("開始日", today_dt - timedelta(days=7))
end_date = st.sidebar.date_input("終了日", today_dt)

export_df = load_data_range(start_date, end_date)
if not export_df.empty:
    # CSV出力。ここに出力されるデータに threshold_set カラムが含まれます
    csv = export_df.to_csv(index=False).encode('utf_8_sig')
    st.sidebar.download_button(label="📥 CSV形式でダウンロード", data=csv, 
                               file_name=f"greenhouse_data_{start_date}.csv", mime="text/csv")
    st.sidebar.info(f"データ件数: {len(export_df)} 件")

# メイン表示エリアのプレースホルダー
placeholder = st.empty()

# シリアル通信準備
ser = None
if not DEBUG_MODE:
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except Exception as e:
        st.error(f"シリアル接続エラー: {e}"); st.stop()

# ==========================================
# 4. メインループ
# ==========================================
loop_interval = 3 

try:
    while True:
        # --- データ取得 ---
        if DEBUG_MODE:
            t = round(random.uniform(20.0, 28.0), 1)
            h = round(random.uniform(85.0, 98.0), 1)
            is_wet = 1 if h > 90 else 0
            time.sleep(loop_interval)
        else:
            line = ser.readline().decode('utf-8').strip()
            if not line: continue
            try:
                t, h, is_wet = map(float, line.split(','))
            except ValueError: continue

        # --- 葉濡れ累積ロジック ---
        now = datetime.now()
        if is_wet == 1:
            st.session_state.lw_total_seconds += loop_interval
            st.session_state.dry_start_time = None
        else:
            if st.session_state.dry_start_time is None:
                st.session_state.dry_start_time = now
            if (now - st.session_state.dry_start_time) > timedelta(minutes=reset_threshold_min):
                st.session_state.lw_total_seconds = 0.0
        
        lw_minutes = round(st.session_state.lw_total_seconds / 60, 2)
        lw_hours = st.session_state.lw_total_seconds / 3600

        # 各種計算
        vpd = calculate_vpd(t, h)
        risk_p = calculate_p(lw_hours, t)
        
        # ★ threshold_setも含めてDBに保存
        save_to_db(t, h, vpd, lw_minutes, risk_p, alert_threshold)
        
        display_df = load_data_range(now - timedelta(days=1), now).tail(200)
        
        # --- 画面更新 ---
        with placeholder.container():
            m1, m2, m3, m4, m5 = st.columns(5)
            st.caption(f"最終更新: {now.strftime('%H:%M:%S')} (現在選択中の閾値: {alert_threshold}%)")
            
            m1.metric("温度", f"{t} °C")
            m2.metric("湿度", f"{h} %")
            m3.metric("VPD", f"{vpd} kPa")
            m4.metric("累積葉濡れ", f"{lw_minutes} 分")
            
            # アラート判定
            is_protected = st.session_state.pesticide_expiry and st.session_state.pesticide_expiry > now
            if is_protected:
                m5.metric("病害リスク P", f"{risk_p} %", delta="散布効果中", delta_color="off")
            else:
                is_alert = risk_p >= alert_threshold
                m5.metric("病害リスク P", f"{risk_p} %", 
                          delta="警戒" if is_alert else "安定", 
                          delta_color="inverse" if is_alert else "normal")

            if not display_df.empty:
                # グラフ表示（略）
                st.plotly_chart(px.line(display_df, x='timestamp', y=['temperature', 'humidity'], 
                                        title="🌡 気温・湿度推移", 
                                        color_discrete_map={"temperature": "#EF553B", "humidity": "#636EFA"}), 
                                use_container_width=True)

                fig_risk = px.line(display_df, x='timestamp', y='risk_p', 
                                   title=f"⚠️ 病害リスク推移 (ライン: {alert_threshold}%)",
                                   color_discrete_sequence=["#AB63FA"]).update_yaxes(range=[0, 100])
                fig_risk.add_hline(y=alert_threshold, line_dash="dash", line_color="red")
                st.plotly_chart(fig_risk, use_container_width=True)

                st.plotly_chart(px.area(display_df, x='timestamp', y='leaf_wetness_min', 
                                        title="🍃 累積葉濡れ時間 (分)", 
                                        color_discrete_sequence=["#00CC96"]), 
                                use_container_width=True)

except KeyboardInterrupt:
    if ser: ser.close()